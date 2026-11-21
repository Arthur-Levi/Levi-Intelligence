import asyncio
import signal
from uuid import uuid4

import pytest

from app.crawler_jobs.contracts import CrawlerJobInput
from app.crawler_jobs.timing import CrawlerTiming
from app.workers import crawler as worker


class Repository:
    def __init__(self, job: CrawlerJobInput | None = None) -> None:
        self.job = job
        self.claimed = asyncio.Event()
        self.claims = 0
        self.results = 0
        self.cancellations = 0

    async def claim_next(self, *, worker_id):
        self.claims += 1
        self.claimed.set()
        job, self.job = self.job, None
        return job

    async def cancellation_requested(self, job_id, tenant_id):
        return False

    async def persist_result(self, job, result):
        self.results += 1

    async def persist_cancellation(self, job):
        self.cancellations += 1


@pytest.mark.asyncio
async def test_idle_worker_exits_after_signal_without_new_claims(monkeypatch) -> None:
    repository = Repository()
    handlers = {}
    disposed = asyncio.Event()

    class Engine:
        async def dispose(self):
            disposed.set()

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, callback: handlers.update({sig: callback})
    )
    monkeypatch.setattr(worker, "create_async_engine", lambda *args, **kwargs: Engine())
    monkeypatch.setattr(worker, "async_sessionmaker", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "PostgresCrawlerJobRepository", lambda factory: repository)
    monkeypatch.setattr(worker.CrawlerTiming, "from_env", lambda: CrawlerTiming(poll_seconds=0.01))
    monkeypatch.setenv("CRAWLER_DATABASE_URL", "postgresql+asyncpg://test@localhost/test")
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(repository.claimed.wait(), 1)
    handlers[signal.SIGTERM]()
    await asyncio.wait_for(task, 1)
    assert disposed.is_set()
    assert repository.claims == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_during_grace", [True, False])
async def test_worker_drains_and_bounds_active_task(monkeypatch, finish_during_grace) -> None:
    job = CrawlerJobInput(
        job_id=uuid4(), tenant_id=uuid4(), company_id=uuid4(),
        website_url="https://example.com", idempotency_key="request-0001",
    )
    repository = Repository(job)
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    handlers = {}
    disposed = asyncio.Event()

    class Engine:
        async def dispose(self):
            disposed.set()

    async def crawl(_url):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        from datetime import UTC, datetime

        return {"status": "verified", "status_code": 200, "checked_at": datetime.now(UTC)}

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, callback: handlers.update({sig: callback})
    )
    monkeypatch.setattr(worker, "create_async_engine", lambda *args, **kwargs: Engine())
    monkeypatch.setattr(worker, "async_sessionmaker", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "PostgresCrawlerJobRepository", lambda factory: repository)
    monkeypatch.setattr(worker, "verify_and_extract", crawl)
    monkeypatch.setattr(worker.CrawlerTiming, "from_env", lambda: CrawlerTiming(
        attempt_seconds=10, cancellation_seconds=0.1,
        shutdown_grace_seconds=0.03, shutdown_cleanup_seconds=0.1, poll_seconds=0.01,
    ))
    monkeypatch.setenv("CRAWLER_DATABASE_URL", "postgresql+asyncpg://test@localhost/test")
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(started.wait(), 1)
    handlers[signal.SIGTERM]()
    handlers[signal.SIGINT]()
    if finish_during_grace:
        release.set()
    await asyncio.wait_for(task, 1)
    assert disposed.is_set()
    assert repository.claims == 1
    assert repository.cancellations == 0
    assert repository.results == int(finish_during_grace)
    assert cancelled.is_set() is not finish_during_grace
