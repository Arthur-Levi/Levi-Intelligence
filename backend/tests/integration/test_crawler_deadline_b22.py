"""PostgreSQL 16 deadline and lease-fencing checks with synthetic crawlers."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_postgres_crawler_lifecycle import add_jobs
from test_postgres_crawler_lifecycle import world as shared_world  # noqa: F401

from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.crawler_jobs.timing import CrawlerTiming
from app.crawler_jobs.worker import execute_crawler_job
from app.models.prospecting import CrawlerJob, CrawlerJobAttempt, CrawlerJobStatus, SourceRecord

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_timed_out_attempt_has_no_partial_result_or_extra_attempt(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    repository = PostgresCrawlerJobRepository(factory)
    claim = await repository.claim_next(tenants[0])
    assert claim is not None
    cancelled = asyncio.Event()

    async def slow(_url):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    assert await asyncio.wait_for(execute_crawler_job(
        claim, repository=repository, crawler=slow,
        timing=CrawlerTiming(attempt_seconds=0.02),
    ), 5) == "failed"
    assert cancelled.is_set()
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        attempt = await session.get(CrawlerJobAttempt, claim.attempt_id)
        sources = await session.scalar(select(func.count()).select_from(SourceRecord).where(
            SourceRecord.tenant_id == tenants[0], SourceRecord.company_id == companies[0]
        ))
    assert row.status == CrawlerJobStatus.FAILED
    assert row.failure_code == "attempt_timeout" and row.result is None
    assert row.attempts == 1 and attempt.failure_code == "attempt_timeout"
    assert sources == 0
    assert await repository.claim_next(tenants[0]) is None


@pytest.mark.asyncio
async def test_expired_lease_rejects_timeout_finalization(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    repository = PostgresCrawlerJobRepository(factory)
    old = await repository.claim_next(tenants[0])
    assert old is not None
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, ids[0], with_for_update=True)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(RuntimeError, match="lease"):
        await repository.persist_failure(old, "attempt_timeout")
    assert await repository.claim_next(tenants[0]) is None
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        attempt = await session.get(CrawlerJobAttempt, old.attempt_id)
    assert row.status == CrawlerJobStatus.QUEUED
    assert row.attempts == 1 and attempt.outcome == "expired"
