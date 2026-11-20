"""Prove one-shot faults land on the intended real PostgreSQL boundaries."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from crawler_faults import FaultPlan, FaultPoint, FaultRepository, InjectedFault, fixture_id
from sqlalchemy import func, select
from test_postgres_crawler_lifecycle import result
from test_postgres_crawler_lifecycle import world as shared_world  # noqa: F401

from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.models.prospecting import CrawlerJob, CrawlerJobAttempt, CrawlerJobStatus, SourceRecord

pytestmark = pytest.mark.integration


async def seeded_job(factory, tenant, user, company, point):
    job_id = fixture_id("r3-a-v1", point.value)
    async with factory.begin() as session:
        session.add(CrawlerJob(
            id=job_id,
            tenant_id=tenant,
            company_id=company,
            created_by_user_id=user,
            website_url="https://example.test",
            idempotency_key=f"r3-a-{point.value}",
            request_fingerprint="a" * 64,
            status=CrawlerJobStatus.QUEUED,
        ))
    return job_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("point", "committed"),
    [
        (FaultPoint.BEFORE_CLAIM, False),
        (FaultPoint.AFTER_ATTEMPT_CREATION, False),
        (FaultPoint.BEFORE_COMMIT, False),
        (FaultPoint.AFTER_CLAIM_COMMIT, True),
    ],
)
async def test_claim_fault_boundaries(shared_world, point, committed):  # noqa: F811
    factory, tenants, users, companies = shared_world
    job_id = await seeded_job(factory, tenants[0], users[0], companies[0], point)
    plan = FaultPlan(point)
    repo = FaultRepository(factory, plan)
    with pytest.raises(InjectedFault, match=point.value):
        await asyncio.wait_for(repo.claim_next(tenants[0]), 5)
    plan.verify()
    async with factory() as session:
        row = await session.get(CrawlerJob, job_id)
        count = await session.scalar(
            select(func.count())
            .select_from(CrawlerJobAttempt)
            .where(CrawlerJobAttempt.job_id == row.id)
        )
    assert row.status == (CrawlerJobStatus.RUNNING if committed else CrawlerJobStatus.QUEUED)
    assert row.attempts == (1 if committed else 0)
    assert count == (1 if committed else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "point",
    [FaultPoint.BEFORE_RENEWAL, FaultPoint.BEFORE_RESULT, FaultPoint.DURING_RESULT,
     FaultPoint.AMBIGUOUS_RESULT_COMMIT],
)
async def test_processing_fault_boundaries(shared_world, point):  # noqa: F811
    factory, tenants, users, companies = shared_world
    job_id = await seeded_job(factory, tenants[0], users[0], companies[0], point)
    claim = await PostgresCrawlerJobRepository(factory).claim_next(tenants[0])
    assert claim is not None
    plan = FaultPlan(point)
    repo = FaultRepository(factory, plan)
    with pytest.raises(InjectedFault, match=point.value):
        if point == FaultPoint.BEFORE_RENEWAL:
            await asyncio.wait_for(repo.heartbeat(claim), 5)
        else:
            await asyncio.wait_for(repo.persist_result(claim, result()), 5)
    plan.verify()
    async with factory() as session:
        row = await session.get(CrawlerJob, job_id)
        sources = await session.scalar(
            select(func.count())
            .select_from(SourceRecord)
            .where(SourceRecord.tenant_id == tenants[0])
        )
    committed = point == FaultPoint.AMBIGUOUS_RESULT_COMMIT
    assert row.status == (CrawlerJobStatus.COMPLETED if committed else CrawlerJobStatus.RUNNING)
    assert sources == int(committed)


@pytest.mark.asyncio
@pytest.mark.parametrize("point", [FaultPoint.DURING_RETRY, FaultPoint.DURING_CANCELLATION])
async def test_recovery_and_cancellation_fault_boundaries(shared_world, point):  # noqa: F811
    factory, tenants, users, companies = shared_world
    job_id = await seeded_job(factory, tenants[0], users[0], companies[0], point)
    claim = await PostgresCrawlerJobRepository(factory).claim_next(tenants[0])
    assert claim is not None
    if point == FaultPoint.DURING_RETRY:
        async with factory.begin() as session:
            row = await session.get(CrawlerJob, job_id, with_for_update=True)
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    plan = FaultPlan(point)
    repo = FaultRepository(factory, plan)
    with pytest.raises(InjectedFault, match=point.value):
        if point == FaultPoint.DURING_RETRY:
            await asyncio.wait_for(repo.claim_next(tenants[0]), 5)
        else:
            await asyncio.wait_for(repo.persist_cancellation(claim), 5)
    plan.verify()
    async with factory() as session:
        row = await session.get(CrawlerJob, job_id)
        attempt = await session.get(CrawlerJobAttempt, claim.attempt_id)
    assert row.status == CrawlerJobStatus.RUNNING
    assert attempt.ended_at is None
