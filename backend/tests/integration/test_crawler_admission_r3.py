"""Real API admission at the exact queue thresholds, with bounded synthetic seeding."""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from crawler_faults import fixture_id
from fastapi import HTTPException
from sqlalchemy import delete, func, insert, select
from test_postgres_crawler_lifecycle import add_jobs, context, request
from test_postgres_crawler_lifecycle import world as shared_world  # noqa: F401

from app.api.v1.endpoints.prospecting import create_company_enrichment_job, enrich_company
from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.models.identity import Tenant
from app.models.prospecting import Company, CrawlerJob, CrawlerJobStatus

pytestmark = pytest.mark.integration


async def admit(factory, tenant, user, company, key):
    ctx = await context(factory, tenant, user)
    async with factory() as session:
        return await create_company_enrichment_job(company, request(), key, ctx, session)


@pytest.mark.asyncio
async def test_tenant_99_to_100_and_last_slot_race(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    await add_jobs(factory, tenants[0], users[0], companies[0], 99)
    gate = asyncio.Event()

    async def contender(index):
        await gate.wait()
        try:
            return await admit(factory, tenants[0], users[0], companies[0],
                               f"r3-tenant-race-{index}-{uuid4().hex}")
        except HTTPException as exc:
            return exc

    tasks = [asyncio.create_task(contender(i)) for i in range(4)]
    gate.set()
    responses = await asyncio.wait_for(asyncio.gather(*tasks), 15)
    accepted = [r for r in responses if not isinstance(r, HTTPException)]
    rejected = [r for r in responses if isinstance(r, HTTPException)]
    assert len(accepted) == 1 and len(rejected) == 3
    assert all(r.status_code == 429 and r.detail == {"code": "crawler_tenant_queue_full"}
               for r in rejected)
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(CrawlerJob).where(
            CrawlerJob.tenant_id == tenants[0], CrawlerJob.status == CrawlerJobStatus.QUEUED))
    assert count == 100


@pytest.mark.asyncio
async def test_duplicate_key_race_at_tenant_capacity(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    await add_jobs(factory, tenants[0], users[0], companies[0], 99)
    key = "r3-duplicate-last-slot"
    gate = asyncio.Event()

    async def contender():
        await gate.wait()
        return await admit(factory, tenants[0], users[0], companies[0], key)

    tasks = [asyncio.create_task(contender()) for _ in range(4)]
    gate.set()
    responses = await asyncio.wait_for(asyncio.gather(*tasks), 15)
    assert len({r.id for r in responses}) == 1
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(CrawlerJob).where(
            CrawlerJob.tenant_id == tenants[0], CrawlerJob.status == CrawlerJobStatus.QUEUED))
    assert count == 100


@pytest.mark.asyncio
async def test_global_9999_to_10000_and_rejection(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    extra_tenants = [fixture_id("r3-global-v1", f"tenant-{i}") for i in range(99)]
    extra_companies = [fixture_id("r3-global-v1", f"company-{i}") for i in range(99)]
    try:
        async with factory.begin() as session:
            await session.execute(insert(Tenant), [
                {"id": tenant, "name": "Synthetic R3 tenant", "slug": f"r3-{tenant.hex}"}
                for tenant in extra_tenants
            ])
            await session.execute(insert(Company), [
                {"id": extra_companies[i], "tenant_id": tenant,
                 "name": "Synthetic R3 company", "normalized_domain": f"r3-{i}.example.test",
                 "website_url": "https://example.test"}
                for i, tenant in enumerate(extra_tenants)
            ])
        rows = []
        for index, tenant in enumerate(extra_tenants):
            for number in range(1 if index == 98 else 100):
                job_id = fixture_id("r3-global-v1", f"job-{index}-{number}")
                rows.append({
                    "id": job_id, "tenant_id": tenant, "company_id": extra_companies[index],
                    "created_by_user_id": users[0], "website_url": "https://example.test",
                    "idempotency_key": f"r3-global-{number}",
                    "request_fingerprint": "a" * 64, "status": CrawlerJobStatus.QUEUED,
                })
                if len(rows) == 500:
                    async with factory.begin() as session:
                        await session.execute(insert(CrawlerJob), rows)
                    rows = []
        if rows:
            async with factory.begin() as session:
                await session.execute(insert(CrawlerJob), rows)
        await add_jobs(factory, tenants[0], users[0], companies[0], 99)
        await add_jobs(factory, tenants[1], users[1], companies[1], 99)
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(CrawlerJob).where(
                CrawlerJob.status == CrawlerJobStatus.QUEUED)) == 9999
        gate = asyncio.Event()

        async def race(tenant, user, company, key):
            await gate.wait()
            try:
                return await admit(factory, tenant, user, company, key)
            except HTTPException as exc:
                return exc

        tasks = [
            asyncio.create_task(race(tenants[0], users[0], companies[0], "r3-global-final-a")),
            asyncio.create_task(race(tenants[1], users[1], companies[1], "r3-global-final-b")),
        ]
        gate.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), 15)
        accepted = [response for response in responses if not isinstance(response, HTTPException)]
        rejected = [response for response in responses if isinstance(response, HTTPException)]
        assert len(accepted) == len(rejected) == 1
        assert accepted[0].status == CrawlerJobStatus.QUEUED
        assert rejected[0].status_code == 429
        assert rejected[0].detail == {"code": "crawler_queue_full"}
        winning_tenant = 0 if responses[0] is accepted[0] else 1
        duplicate = await asyncio.wait_for(admit(
            factory, tenants[winning_tenant], users[winning_tenant], companies[winning_tenant],
            f"r3-global-final-{'a' if winning_tenant == 0 else 'b'}"), 10)
        assert duplicate.id == accepted[0].id
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(CrawlerJob).where(
                CrawlerJob.status == CrawlerJobStatus.QUEUED)) == 10000
        legacy_context = await context(factory, tenants[winning_tenant], users[winning_tenant])
        async with factory() as session:
            with pytest.raises(HTTPException) as legacy_error:
                await enrich_company(
                    companies[winning_tenant], request(), "b1-global-over-capacity",
                    legacy_context, session,
                )
        assert legacy_error.value.status_code == 429
        assert legacy_error.value.detail == {"code": "crawler_queue_full"}
    finally:
        async with factory.begin() as session:
            await session.execute(delete(CrawlerJob).where(CrawlerJob.tenant_id.in_(extra_tenants)))
            await session.execute(delete(Company).where(Company.tenant_id.in_(extra_tenants)))
            await session.execute(delete(Tenant).where(Tenant.id.in_(extra_tenants)))


@pytest.mark.asyncio
async def test_concurrent_admission_and_claim_keeps_distinct_caps(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    await add_jobs(factory, tenants[0], users[0], companies[0], 99)
    await add_jobs(factory, tenants[1], users[1], companies[1], 2)
    repo = PostgresCrawlerJobRepository(factory)
    gate = asyncio.Event()

    async def submit(tenant, user, company, key):
        await gate.wait()
        return await admit(factory, tenant, user, company, key)

    async def claim():
        await gate.wait()
        return await repo.claim_next()

    tasks = [
        asyncio.create_task(submit(tenants[0], users[0], companies[0], "r3-claim-race-a")),
        asyncio.create_task(submit(tenants[1], users[1], companies[1], "r3-claim-race-b")),
        asyncio.create_task(claim()), asyncio.create_task(claim()),
    ]
    gate.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), 15)
    assert results[0].status == results[1].status == CrawlerJobStatus.QUEUED
    assert all(claim is not None for claim in results[2:])
    async with factory() as session:
        queued = await session.scalar(select(func.count()).select_from(CrawlerJob).where(
            CrawlerJob.tenant_id == tenants[0], CrawlerJob.status == CrawlerJobStatus.QUEUED))
        running = await session.scalar(select(func.count()).select_from(CrawlerJob).where(
            CrawlerJob.status == CrawlerJobStatus.RUNNING))
    assert queued <= 100 and running == 2


@pytest.mark.asyncio
async def test_two_api_processes_race_last_tenant_slot(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    await add_jobs(factory, tenants[0], users[0], companies[0], 99)
    child_path = Path(__file__).with_name("crawler_admission_child.py")
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    children = []
    try:
        for index in range(2):
            children.append(await asyncio.create_subprocess_exec(
                sys.executable, str(child_path), str(tenants[0]), str(users[0]),
                str(companies[0]), f"r3-api-process-{index}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=Path(__file__).resolve().parents[2], env=child_env,
            ))
        outputs = await asyncio.wait_for(
            asyncio.gather(*(child.communicate() for child in children)), 12)
        assert all(child.returncode == 0 for child in children)
        responses = [json.loads(stdout) for stdout, _stderr in outputs]
        assert len([r for r in responses if r["status"] == "accepted"]) == 1
        rejected = [r for r in responses if r["status"] == "rejected"]
        assert rejected == [{"status": "rejected", "code": 429,
                             "detail": {"code": "crawler_tenant_queue_full"}}]
        async with factory() as session:
            count = await session.scalar(select(func.count()).select_from(CrawlerJob).where(
                CrawlerJob.tenant_id == tenants[0],
                CrawlerJob.status == CrawlerJobStatus.QUEUED))
        assert count == 100
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
                await asyncio.wait_for(child.wait(), 5)
