"""Both HTTP admission URLs share one durable PostgreSQL crawler contract."""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_postgres_crawler_lifecycle import add_jobs
from test_postgres_crawler_lifecycle import world as shared_world  # noqa: F401

from app.core.security import create_access_token
from app.db import get_db_session
from app.intelligence.crawler.http_transport import SecureHTTPTransport
from app.main import app
from app.models.identity import AuditEvent, Membership, MembershipRole
from app.models.prospecting import Company, CrawlerJob, CrawlerJobStatus, SourceRecord
from app.services import company_enrichment

pytestmark = pytest.mark.integration


@asynccontextmanager
async def api_client(factory):
    async def test_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = test_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db_session, None)


def headers(user, tenant, key):
    return {"Authorization": f"Bearer {create_access_token(user_id=user, tenant_id=tenant)}",
            "Idempotency-Key": key}


@pytest.mark.asyncio
async def test_routes_share_durable_job_without_api_fetch_and_support_polling(
    shared_world,  # noqa: F811
    monkeypatch,
):
    factory, tenants, users, companies = shared_world

    async def forbidden_fetch(*args, **kwargs):
        pytest.fail("API invoked website crawler")

    monkeypatch.setattr(company_enrichment, "verify_and_extract", forbidden_fetch)
    monkeypatch.setattr(SecureHTTPTransport, "fetch", forbidden_fetch)
    async with api_client(factory) as client:
        base = f"/api/v1/companies/{companies[0]}/enrich"
        auth = headers(users[0], tenants[0], "b1-same-request")
        legacy = await asyncio.wait_for(client.post(base, headers=auth), 10)
        canonical = await asyncio.wait_for(client.post(base + "/jobs", headers=auth), 10)
        repeat = await asyncio.wait_for(client.post(base, headers=auth), 10)
        assert [response.status_code for response in (legacy, canonical, repeat)] == [202] * 3
        jobs = [response.json() for response in (legacy, canonical, repeat)]
        assert jobs[0] == jobs[1] == jobs[2]
        assert jobs[0]["company_id"] == str(companies[0])
        assert jobs[0]["status"] == "queued" and jobs[0]["result"] is None
        assert jobs[0]["finished_at"] is None
        poll = await asyncio.wait_for(client.get(
            f"/api/v1/crawler-jobs/{jobs[0]['id']}", headers=auth), 10)
        assert poll.status_code == 200 and poll.json()["id"] == jobs[0]["id"]
        other_poll = await asyncio.wait_for(client.get(
            f"/api/v1/crawler-jobs/{jobs[0]['id']}",
            headers=headers(users[1], tenants[1], "b1-other-request")), 10)
        assert other_poll.status_code == 404

    async with factory() as session:
        company = await session.get(Company, companies[0])
        jobs_count = await session.scalar(select(func.count()).select_from(CrawlerJob).where(
            CrawlerJob.tenant_id == tenants[0]))
        source_count = await session.scalar(select(func.count()).select_from(SourceRecord).where(
            SourceRecord.tenant_id == tenants[0],
            SourceRecord.source_type == "website_verification"))
        audits = await session.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.tenant_id == tenants[0], AuditEvent.action == "company.enrichment_queued"))
    assert jobs_count == 1 and source_count == 0 and audits == 1
    assert company.enrichment_status == "queued" and company.website_status == "unknown"
    assert company.website_checked_at is None


@pytest.mark.asyncio
async def test_legacy_auth_tenant_missing_url_and_fingerprint_conflict(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    extra_id = uuid4()
    async with factory.begin() as session:
        session.add(Company(
            id=extra_id, tenant_id=tenants[0], name="Second synthetic company",
            normalized_domain=f"{extra_id.hex}.example.test", website_url="https://example.test",
        ))
    async with api_client(factory) as client:
        route = f"/api/v1/companies/{companies[0]}/enrich"
        auth = headers(users[0], tenants[0], "b1-conflict-key")
        assert (await client.post(route)).status_code == 401
        assert (await client.post(
            f"/api/v1/companies/{companies[1]}/enrich", headers=auth)).status_code == 404
        assert (await client.post(
            f"/api/v1/companies/{uuid4()}/enrich", headers=auth)).status_code == 404
        assert (await client.post(route, headers={"Authorization": auth["Authorization"]})
                ).status_code == 422
        first = await client.post(route, headers=auth)
        assert first.status_code == 202
        conflict = await client.post(
            f"/api/v1/companies/{extra_id}/enrich/jobs", headers=auth)
        assert conflict.status_code == 409
        async with factory.begin() as session:
            member = await session.scalar(select(Membership).where(
                Membership.user_id == users[0], Membership.tenant_id == tenants[0]))
            member.role = MembershipRole.VIEWER
        assert (await client.post(route, headers=headers(
            users[0], tenants[0], "b1-viewer-request"))).status_code == 403

    async with factory.begin() as session:
        company = await session.get(Company, companies[0])
        company.website_url = None
    async with api_client(factory) as client:
        response = await client.post(
            f"/api/v1/companies/{companies[0]}/enrich",
            headers=headers(users[1], tenants[1], "b1-no-website"))
        assert response.status_code == 404  # Other tenant still cannot see the row.
        async with factory.begin() as session:
            member = await session.scalar(select(Membership).where(
                Membership.user_id == users[0], Membership.tenant_id == tenants[0]))
            member.role = MembershipRole.OWNER
        response = await client.post(
            f"/api/v1/companies/{companies[0]}/enrich",
            headers=headers(users[0], tenants[0], "b1-no-website"))
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_legacy_route_obeys_tenant_queue_capacity(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    await add_jobs(factory, tenants[0], users[0], companies[0], 99)
    async with api_client(factory) as client:
        base = f"/api/v1/companies/{companies[0]}/enrich"
        winning = await client.post(base, headers=headers(users[0], tenants[0], "b1-last-slot"))
        rejected = await client.post(base, headers=headers(users[0], tenants[0], "b1-over-slot"))
        canonical = await client.post(
            base + "/jobs", headers=headers(users[0], tenants[0], "b1-over-slot-2"))
    assert winning.status_code == 202
    assert rejected.status_code == canonical.status_code == 429
    assert rejected.json()["detail"] == canonical.json()["detail"] == {
        "code": "crawler_tenant_queue_full"}
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(CrawlerJob).where(
            CrawlerJob.tenant_id == tenants[0], CrawlerJob.status == CrawlerJobStatus.QUEUED))
    assert count == 100
