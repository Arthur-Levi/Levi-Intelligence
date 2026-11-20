"""Dedicated, bounded crawler worker process."""

import asyncio
import logging
import os
import signal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.crawler_jobs.worker import execute_crawler_job
from app.services.company_enrichment import verify_and_extract

logger = logging.getLogger("levi.crawler_worker")


async def run() -> None:
    database_url = os.environ.get("CRAWLER_DATABASE_URL")
    if not database_url:
        raise RuntimeError("CRAWLER_DATABASE_URL is required")
    concurrency = max(1, min(8, int(os.environ.get("CRAWLER_WORKER_CONCURRENCY", "2"))))
    poll_seconds = max(0.2, min(10.0, float(os.environ.get("CRAWLER_POLL_SECONDS", "1"))))
    engine = create_async_engine(database_url, pool_pre_ping=True, pool_size=concurrency)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgresCrawlerJobRepository(factory)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    semaphore = asyncio.Semaphore(concurrency)
    active: set[asyncio.Task[None]] = set()

    async def work() -> None:
        async with semaphore:
            job = await repository.claim_next()
            if job is None:
                return
            logger.info(
                "crawler_job_claimed",
                extra={"job_id": str(job.job_id), "tenant_id": str(job.tenant_id)},
            )
            await execute_crawler_job(job, repository=repository, crawler=verify_and_extract)

    try:
        while not stopping.is_set():
            active.difference_update(task for task in active if task.done())
            if len(active) < concurrency:
                task = asyncio.create_task(work())
                active.add(task)
                await asyncio.sleep(0)
                if task.done():
                    await task
                    if task.exception() is None:
                        await asyncio.sleep(poll_seconds)
            else:
                await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
        if active:
            await asyncio.gather(*active, return_exceptions=True)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
    asyncio.run(run())
