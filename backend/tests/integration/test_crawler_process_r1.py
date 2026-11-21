"""Real process executor, PostgreSQL fencing and synthetic URL handoff."""

import asyncio
import time
from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_postgres_crawler_lifecycle import add_jobs
from test_postgres_crawler_lifecycle import world as shared_world  # noqa: F401

from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.crawler_jobs.process_executor import ProcessCrawler
from app.crawler_jobs.timing import CrawlerTiming
from app.crawler_jobs.worker import execute_crawler_job
from app.models.prospecting import CrawlerJob, CrawlerJobAttempt, CrawlerJobStatus, SourceRecord

pytestmark = pytest.mark.integration
FIXTURE = Path(__file__).resolve().parents[1] / "crawler_process_fixture.py"


@pytest.mark.asyncio
async def test_process_result_is_fenced_and_child_sees_no_db_socket(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, ids[0], with_for_update=True)
        row.website_url = "https://example.test/inspect-env?marker=/tmp/r1-unused-marker"
    repository = PostgresCrawlerJobRepository(factory)
    job = await repository.claim_next(tenants[0])
    assert job is not None and job.attempt_id is not None
    acquired = time.monotonic()
    crawler = ProcessCrawler(
        attempt_id=job.attempt_id, deadline=acquired + 2,
        termination_seconds=0.2, executor_path=FIXTURE,
    )
    state = await asyncio.wait_for(execute_crawler_job(
        job, repository=repository, crawler=crawler,
        timing=CrawlerTiming(attempt_seconds=2), acquired_at=acquired,
    ), 5)
    assert state == "completed"
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        attempt = await session.get(CrawlerJobAttempt, job.attempt_id)
        sources = await session.scalar(select(func.count()).select_from(SourceRecord).where(
            SourceRecord.company_id == companies[0]
        ))
    assert row.status == CrawlerJobStatus.COMPLETED and row.attempts == 1
    assert row.result["status_code"] == 200 and attempt.outcome == "completed"
    assert sources == 1


@pytest.mark.asyncio
async def test_blocked_process_deadline_commits_no_result(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, ids[0], with_for_update=True)
        row.website_url = "https://example.test/block"
    repository = PostgresCrawlerJobRepository(factory)
    job = await repository.claim_next(tenants[0])
    assert job is not None and job.attempt_id is not None
    acquired = time.monotonic()
    crawler = ProcessCrawler(
        attempt_id=job.attempt_id, deadline=acquired + 0.3,
        termination_seconds=0.2, executor_path=FIXTURE,
    )
    state = await asyncio.wait_for(execute_crawler_job(
        job, repository=repository, crawler=crawler,
        timing=CrawlerTiming(attempt_seconds=0.3), acquired_at=acquired,
    ), 5)
    assert state == "failed"
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        attempt = await session.get(CrawlerJobAttempt, job.attempt_id)
        sources = await session.scalar(select(func.count()).select_from(SourceRecord).where(
            SourceRecord.company_id == companies[0]
        ))
    assert row.status == CrawlerJobStatus.FAILED and row.failure_code == "attempt_timeout"
    assert row.result is None and attempt.failure_code == "attempt_timeout" and sources == 0
    assert crawler.child_pid is not None
