"""Real PostgreSQL lifecycle races; every contender uses its own connection."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.dependencies import TenantContext
from app.api.v1.endpoints.prospecting import (
    cancel_crawler_job,
    create_company_enrichment_job,
    get_crawler_job,
)
from app.crawler_jobs.contracts import CrawlerJobResult
from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.models.identity import AuditEvent, Membership, MembershipRole, Tenant, User
from app.models.prospecting import Company, CrawlerJob, CrawlerJobStatus, SourceRecord

pytestmark = pytest.mark.integration


@pytest.fixture
async def world():
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"], pool_size=16)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenants = [uuid4(), uuid4()]
    users = [uuid4(), uuid4()]
    companies = [uuid4(), uuid4()]
    try:
        async with factory.begin() as session:
            for index in range(2):
                session.add(
                    Tenant(id=tenants[index], name="Gate tenant", slug=f"gate-{tenants[index].hex}")
                )
                session.add(
                    User(
                        id=users[index],
                        email=f"{users[index].hex}@example.test",
                        full_name="Gate user",
                        password_hash="synthetic",
                    )
                )
                session.add(
                    Membership(
                        user_id=users[index], tenant_id=tenants[index], role=MembershipRole.OWNER
                    )
                )
                session.add(
                    Company(
                        id=companies[index],
                        tenant_id=tenants[index],
                        name="Gate company",
                        normalized_domain=f"{companies[index].hex}.example.test",
                        website_url="https://example.test",
                    )
                )
        yield factory, tenants, users, companies
    finally:
        async with factory.begin() as session:
            await session.execute(delete(AuditEvent).where(AuditEvent.tenant_id.in_(tenants)))
            await session.execute(delete(SourceRecord).where(SourceRecord.tenant_id.in_(tenants)))
            await session.execute(delete(CrawlerJob).where(CrawlerJob.tenant_id.in_(tenants)))
            await session.execute(delete(Company).where(Company.tenant_id.in_(tenants)))
            await session.execute(delete(Membership).where(Membership.tenant_id.in_(tenants)))
            await session.execute(delete(User).where(User.id.in_(users)))
            await session.execute(delete(Tenant).where(Tenant.id.in_(tenants)))
        await engine.dispose()


async def add_jobs(factory, tenant, user, company, count=1):
    ids = [uuid4() for _ in range(count)]
    async with factory.begin() as session:
        for job_id in ids:
            session.add(
                CrawlerJob(
                    id=job_id,
                    tenant_id=tenant,
                    company_id=company,
                    created_by_user_id=user,
                    website_url="https://example.test",
                    idempotency_key=f"gate-{job_id.hex}",
                    request_fingerprint="a" * 64,
                    status=CrawlerJobStatus.QUEUED,
                )
            )
    return ids


def result():
    return CrawlerJobResult(status="verified", status_code=200, checked_at=datetime.now(UTC))


async def context(factory, tenant, user):
    async with factory() as session:
        return TenantContext(
            user=await session.get(User, user),
            tenant=await session.get(Tenant, tenant),
            membership=await session.scalar(
                select(Membership).where(Membership.user_id == user, Membership.tenant_id == tenant)
            ),
        )


def request():
    return Request({"type": "http", "client": ("127.0.0.1", 1), "headers": []})


@pytest.mark.asyncio
async def test_claim_races_and_repeated_disputes(world):
    factory, tenants, users, companies = world
    repo = PostgresCrawlerJobRepository(factory)
    ids = await add_jobs(factory, tenants[0], users[0], companies[0], 3)
    for _ in range(3):
        gate = asyncio.Event()

        async def contender(gate=gate):
            await gate.wait()
            return await repo.claim_next(tenants[0])

        contenders = [asyncio.create_task(contender()) for _ in range(10)]
        gate.set()
        claims = [item for item in await asyncio.gather(*contenders) if item]
        assert len(claims) == (2 if _ == 0 else 0)
        assert {item.job_id for item in claims} <= set(ids)
    async with factory() as session:
        rows = (await session.scalars(select(CrawlerJob).where(CrawlerJob.id.in_(ids)))).all()
        assert (
            sum(row.attempts == 1 and row.status == CrawlerJobStatus.RUNNING for row in rows) == 2
        )
        assert sum(row.attempts == 0 and row.status == CrawlerJobStatus.QUEUED for row in rows) == 1


@pytest.mark.asyncio
async def test_skip_locked_and_rollback_before_commit(world):
    factory, tenants, users, companies = world
    repo = PostgresCrawlerJobRepository(factory)
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    async with factory() as holder:
        async with holder.begin():
            row = await holder.scalar(
                select(CrawlerJob).where(CrawlerJob.id == ids[0]).with_for_update()
            )
            assert row is not None
            assert await asyncio.wait_for(repo.claim_next(tenants[0]), 5) is None
            row.status = CrawlerJobStatus.RUNNING
            row.attempts = 1
            row.lease_token = uuid4()
            row.lease_expires_at = datetime.now(UTC) + timedelta(seconds=90)
            await holder.flush()
            await holder.rollback()
    claim = await repo.claim_next(tenants[0])
    assert claim is not None and claim.job_id == ids[0]
    async with factory() as session:
        assert (await session.get(CrawlerJob, ids[0])).attempts == 1


@pytest.mark.asyncio
async def test_tenant_and_attempt_fencing(world):
    factory, tenants, users, companies = world
    repo = PostgresCrawlerJobRepository(factory)
    ids = await add_jobs(factory, tenants[1], users[1], companies[1])
    assert await repo.claim_next(tenants[0]) is None
    claim = await repo.claim_next(tenants[1])
    assert claim is not None and claim.job_id == ids[0]
    forged = claim.model_copy(update={"tenant_id": tenants[0]})
    other_worker = claim.model_copy(update={"lease_token": uuid4()})
    assert not await repo.heartbeat(forged)
    assert not await repo.heartbeat(other_worker)
    with pytest.raises(RuntimeError, match="lease"):
        await repo.persist_result(forged, result())
    with pytest.raises(RuntimeError, match="lease"):
        await repo.persist_result(other_worker, result())
    with pytest.raises(HTTPException) as exc:
        async with factory() as session:
            await get_crawler_job(ids[0], await context(factory, tenants[0], users[0]), session)
    assert exc.value.status_code == 404
    assert await repo.heartbeat(claim)
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, ids[0], with_for_update=True)
        row.lease_expires_at = datetime.now(UTC) - timedelta(microseconds=1)
    assert not await repo.heartbeat(claim)
    assert await repo.claim_next(tenants[1]) is None  # backoff applies
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, ids[0], with_for_update=True)
        row.next_attempt_at = datetime.now(UTC) - timedelta(microseconds=1)
    current = await repo.claim_next(tenants[1])
    assert current is not None and current.lease_token != claim.lease_token
    with pytest.raises(RuntimeError, match="lease"):
        await repo.persist_result(claim, result())
    await repo.persist_result(current, result())
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        assert row.status == CrawlerJobStatus.COMPLETED and row.attempts == 2
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SourceRecord)
                .where(SourceRecord.tenant_id == tenants[1])
            )
            == 1
        )


@pytest.mark.asyncio
async def test_cancel_revokes_running_lease_and_prevents_result(world):
    factory, tenants, users, companies = world
    repo = PostgresCrawlerJobRepository(factory)
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    claim = await repo.claim_next(tenants[0])
    assert claim is not None
    ctx = await context(factory, tenants[0], users[0])
    with pytest.raises(HTTPException) as exc:
        async with factory() as session:
            await cancel_crawler_job(
                ids[0], request(), await context(factory, tenants[1], users[1]), session
            )
    assert exc.value.status_code == 404
    async with factory() as session:
        response = await cancel_crawler_job(ids[0], request(), ctx, session)
    assert response.status == CrawlerJobStatus.CANCELLED
    assert not await repo.heartbeat(claim)
    with pytest.raises(RuntimeError, match="lease"):
        await repo.persist_result(claim, result())
    with pytest.raises(RuntimeError, match="lease"):
        await repo.persist_failure(claim, "crawler_error")
    assert await repo.claim_next(tenants[0]) is None
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SourceRecord)
                .where(SourceRecord.tenant_id == tenants[0])
            )
            == 0
        )


@pytest.mark.asyncio
async def test_expired_cancel_and_retry_exhaustion(world):
    factory, tenants, users, companies = world
    repo = PostgresCrawlerJobRepository(factory)
    ids = await add_jobs(factory, tenants[0], users[0], companies[0], 2)
    async with factory.begin() as session:
        for index, job_id in enumerate(ids):
            row = await session.get(CrawlerJob, job_id)
            row.status = CrawlerJobStatus.RUNNING
            row.attempts = 5 if index else 1
            row.lease_token = uuid4()
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            if index == 0:
                row.cancel_requested_at = datetime.now(UTC)
    assert await repo.claim_next(tenants[0]) is None
    async with factory() as session:
        cancelled, exhausted = [await session.get(CrawlerJob, job_id) for job_id in ids]
        assert cancelled.status == CrawlerJobStatus.CANCELLED
        assert exhausted.status == CrawlerJobStatus.FAILED
        assert exhausted.failure_code == "retry_exhausted"


@pytest.mark.asyncio
async def test_concurrent_idempotent_admission_and_conflict(world):
    factory, tenants, users, companies = world
    ctx = await context(factory, tenants[0], users[0])
    key = f"gate-{uuid4().hex}"
    gate = asyncio.Event()

    async def submit():
        async with factory() as session:
            await gate.wait()
            return await create_company_enrichment_job(companies[0], request(), key, ctx, session)

    tasks = [asyncio.create_task(submit()) for _ in range(5)]
    gate.set()
    responses = await asyncio.gather(*tasks)
    assert len({response.id for response in responses}) == 1
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CrawlerJob)
                .where(CrawlerJob.tenant_id == tenants[0])
            )
            == 1
        )
    async with factory() as session:
        other = await create_company_enrichment_job(
            companies[1], request(), key, await context(factory, tenants[1], users[1]), session
        )
    assert other.id != responses[0].id  # separate tenant namespace
    async with factory.begin() as session:
        company = await session.get(Company, companies[0])
        company.website_url = "https://changed.example.test"
    async with factory() as session:
        with pytest.raises(HTTPException) as exc:
            await create_company_enrichment_job(companies[0], request(), key, ctx, session)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_tenant_queue_capacity_and_release(world):
    factory, tenants, users, companies = world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0], 100)
    ctx = await context(factory, tenants[0], users[0])
    key = f"gate-{uuid4().hex}"
    async with factory() as session:
        with pytest.raises(HTTPException) as exc:
            await create_company_enrichment_job(companies[0], request(), key, ctx, session)
    assert exc.value.status_code == 429
    assert exc.value.detail == {"code": "crawler_tenant_queue_full"}
    async with factory() as session:
        other = await create_company_enrichment_job(
            companies[1],
            request(),
            key,
            await context(factory, tenants[1], users[1]),
            session,
        )
    assert other.status == CrawlerJobStatus.QUEUED
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, ids[0])
        row.status = CrawlerJobStatus.CANCELLED
    async with factory() as session:
        accepted = await create_company_enrichment_job(companies[0], request(), key, ctx, session)
    assert accepted.status == CrawlerJobStatus.QUEUED


@pytest.mark.asyncio
async def test_global_and_tenant_active_capacity(world):
    factory, tenants, users, companies = world
    repo = PostgresCrawlerJobRepository(factory)
    ids = await add_jobs(factory, tenants[0], users[0], companies[0], 4)
    ids += await add_jobs(factory, tenants[1], users[1], companies[1], 4)
    claims = [await repo.claim_next() for _ in range(9)]
    valid = [claim for claim in claims if claim is not None]
    assert len(valid) == 4  # two active slots per tenant
    assert len({claim.job_id for claim in valid}) == 4
    assert await repo.claim_next() is None
    async with factory() as session:
        rows = (await session.scalars(select(CrawlerJob).where(CrawlerJob.id.in_(ids)))).all()
        assert sum(row.status == CrawlerJobStatus.RUNNING for row in rows) == 4
    async with factory.begin() as session:
        rows = (await session.scalars(select(CrawlerJob).where(CrawlerJob.id.in_(ids)))).all()
        for row in rows:
            row.status = CrawlerJobStatus.RUNNING
            row.attempts = 1
            row.lease_token = uuid4()
            row.lease_expires_at = datetime.now(UTC) + timedelta(seconds=90)
    await add_jobs(factory, tenants[0], users[0], companies[0])
    assert await repo.claim_next() is None  # eight active slots globally
