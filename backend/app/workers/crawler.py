"""Dedicated, bounded crawler worker process."""

import asyncio
import logging
import os
import signal
import time
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.crawler_jobs.timing import CrawlerTiming
from app.crawler_jobs.worker import NonCooperativeCrawlerError, execute_crawler_job
from app.services.company_enrichment import verify_and_extract

logger = logging.getLogger("levi.crawler_worker")


async def run() -> None:
    timing = CrawlerTiming.from_env()
    database_url = os.environ.get("CRAWLER_DATABASE_URL")
    if not database_url:
        raise RuntimeError("CRAWLER_DATABASE_URL is required")
    concurrency = max(1, min(8, int(os.environ.get("CRAWLER_WORKER_CONCURRENCY", "2"))))
    engine = create_async_engine(database_url, pool_pre_ping=True, pool_size=concurrency)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgresCrawlerJobRepository(factory)
    worker_id = uuid4()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    shutdown_started: float | None = None

    def begin_shutdown() -> None:
        nonlocal shutdown_started
        if shutdown_started is None:
            shutdown_started = time.monotonic()
            logger.info("crawler_worker_draining")
            stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, begin_shutdown)
    semaphore = asyncio.Semaphore(concurrency)
    active: set[asyncio.Task[None]] = set()

    async def work() -> None:
        async with semaphore:
            if stopping.is_set():
                return
            job = await repository.claim_next(worker_id=worker_id)
            if job is None:
                return
            acquired_at = time.monotonic()
            if stopping.is_set():
                # The committed lease remains durable and will be recovered.
                return
            logger.info(
                "crawler_job_claimed",
                extra={
                    "job_id": str(job.job_id),
                    "tenant_id": str(job.tenant_id),
                    "attempt_id": str(job.attempt_id),
                    "worker_id": str(worker_id),
                },
            )
            await execute_crawler_job(
                job, repository=repository, crawler=verify_and_extract,
                timing=timing, acquired_at=acquired_at,
            )

    def check_finished() -> None:
        finished = {task for task in active if task.done()}
        active.difference_update(finished)
        for task in finished:
            if task.cancelled():
                continue
            error = task.exception()
            if isinstance(error, NonCooperativeCrawlerError):
                logger.critical("crawler_child_ignored_cancellation")
                os._exit(1)
            if error is not None:
                # Exception text can contain a URL or response data.
                logger.error("crawler_worker_task_failed")

    async def bounded_wait(tasks: set[asyncio.Task[None]], seconds: float) -> None:
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=seconds)
        if pending:
            logger.critical("crawler_worker_shutdown_budget_exhausted")
            os._exit(1)

    try:
        while not stopping.is_set():
            check_finished()
            if len(active) < concurrency:
                task = asyncio.create_task(work())
                active.add(task)
            stop_waiter = asyncio.create_task(stopping.wait())
            try:
                await asyncio.wait(
                    active | {stop_waiter},
                    timeout=timing.poll_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                stop_waiter.cancel()
                await asyncio.gather(stop_waiter, return_exceptions=True)
            check_finished()
        if active:
            assert shutdown_started is not None
            grace_left = max(0, shutdown_started + timing.shutdown_grace_seconds - time.monotonic())
            _done, pending = await asyncio.wait(active, timeout=grace_left)
            for task in pending:
                task.cancel()
            await bounded_wait(pending, timing.shutdown_cleanup_seconds)
            check_finished()
    finally:
        disposal = asyncio.create_task(engine.dispose())
        await bounded_wait({disposal}, timing.shutdown_cleanup_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
    asyncio.run(run())
