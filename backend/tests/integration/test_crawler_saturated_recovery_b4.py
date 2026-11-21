"""Capacity and ownership checks for expired crawler leases."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_postgres_crawler_lifecycle import add_jobs
from test_postgres_crawler_lifecycle import world as shared_world  # noqa: F401

from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.models.prospecting import CrawlerJob, CrawlerJobStatus

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_expired_lease_does_not_overflow_tenant_queue(shared_world):  # noqa: F811
    """Expected to fail against B.3: recovery makes the 101st queued row."""
    factory, tenants, users, companies = shared_world
    repo = PostgresCrawlerJobRepository(factory)
    expired_id = (await add_jobs(factory, tenants[0], users[0], companies[0]))[0]
    claim = await repo.claim_next(tenants[0])
    assert claim is not None and claim.job_id == expired_id
    await add_jobs(factory, tenants[0], users[0], companies[0], 100)
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, expired_id, with_for_update=True)
        assert row is not None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await repo.claim_next(tenants[1])
    async with factory() as session:
        queued = await session.scalar(select(func.count()).select_from(CrawlerJob).where(
            CrawlerJob.tenant_id == tenants[0], CrawlerJob.status == CrawlerJobStatus.QUEUED,
        ))
    assert queued == 100
