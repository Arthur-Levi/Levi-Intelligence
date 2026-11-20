"""Synthetic external effect: repeatable call, idempotent receiver, one DB result."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_postgres_crawler_lifecycle import add_jobs, result
from test_postgres_crawler_lifecycle import world as shared_world  # noqa: F401

from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.models.prospecting import CrawlerJob, CrawlerJobStatus, SourceRecord

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_replayed_synthetic_effect_and_single_committed_result(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    repo = PostgresCrawlerJobRepository(factory)
    first = await repo.claim_next(tenants[0])
    assert first is not None
    calls = []
    applied = set()

    def synthetic_receiver(key):
        calls.append(key)
        applied.add(key)

    synthetic_receiver(first.idempotency_key)
    # Work was interrupted after the effect but before a database result commit.
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, ids[0], with_for_update=True)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await repo.claim_next(tenants[0]) is None
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, ids[0], with_for_update=True)
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    second = await repo.claim_next(tenants[0])
    assert second is not None and second.attempt_id != first.attempt_id
    synthetic_receiver(second.idempotency_key)
    await repo.persist_result(second, result())
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        sources = await session.scalar(select(func.count()).select_from(SourceRecord).where(
            SourceRecord.tenant_id == tenants[0]))
    assert row.status == CrawlerJobStatus.COMPLETED
    assert len(calls) == 2 and calls[0] == calls[1]
    assert len(applied) == 1 and sources == 1
