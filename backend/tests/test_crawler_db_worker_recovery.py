import asyncio
import signal
import time

import pytest

from app.crawler_jobs.postgres import CrawlerDatabaseError
from app.crawler_jobs.timing import CrawlerTiming
from app.workers import crawler as worker


@pytest.mark.asyncio
async def test_database_outage_claims_back_off_and_shutdown_stops_retry(monkeypatch) -> None:
    claim_times: list[float] = []
    second_claim = asyncio.Event()
    handlers = {}
    disposed = asyncio.Event()

    class Engine:
        async def dispose(self):
            disposed.set()

    class Repository:
        async def claim_next(self, *, worker_id):
            claim_times.append(time.monotonic())
            if len(claim_times) == 2:
                second_claim.set()
            raise CrawlerDatabaseError("synthetic outage")

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, callback: handlers.update({sig: callback})
    )
    monkeypatch.setattr(worker, "create_crawler_engine", lambda *args, **kwargs: Engine())
    monkeypatch.setattr(worker, "async_sessionmaker", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker, "PostgresCrawlerJobRepository", lambda *args, **kwargs: Repository()
    )
    monkeypatch.setattr(worker.CrawlerTiming, "from_env", lambda: CrawlerTiming(poll_seconds=0.01))
    monkeypatch.setattr(worker, "MAX_DATABASE_BACKOFF_SECONDS", 0.2)
    monkeypatch.setenv("CRAWLER_DATABASE_URL", "postgresql+asyncpg://test@localhost/test")
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(second_claim.wait(), 2)
    handlers[signal.SIGTERM]()
    await asyncio.wait_for(task, 1)
    assert disposed.is_set()
    assert len(claim_times) == 2
    assert claim_times[1] - claim_times[0] >= 0.18


@pytest.mark.asyncio
async def test_shutdown_cancels_a_blocked_database_claim(monkeypatch) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    handlers = {}
    disposed = asyncio.Event()

    class Engine:
        async def dispose(self):
            disposed.set()

    class Repository:
        async def claim_next(self, *, worker_id):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, callback: handlers.update({sig: callback})
    )
    monkeypatch.setattr(worker, "create_crawler_engine", lambda *args, **kwargs: Engine())
    monkeypatch.setattr(worker, "async_sessionmaker", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker, "PostgresCrawlerJobRepository", lambda *args, **kwargs: Repository()
    )
    monkeypatch.setattr(worker.CrawlerTiming, "from_env", lambda: CrawlerTiming(
        shutdown_grace_seconds=0.02, shutdown_cleanup_seconds=0.1,
        cancellation_seconds=0.1, poll_seconds=0.01,
    ))
    monkeypatch.setenv("CRAWLER_DATABASE_URL", "postgresql+asyncpg://test@localhost/test")
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(started.wait(), 1)
    handlers[signal.SIGTERM]()
    await asyncio.wait_for(task, 1)
    assert cancelled.is_set() and disposed.is_set()
