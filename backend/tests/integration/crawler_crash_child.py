"""Child process used only by bounded PostgreSQL crash tests."""

import asyncio
import json
import os
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.crawler_jobs.worker import execute_crawler_job


async def main() -> None:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"], pool_size=1, max_overflow=0)
    try:
        repo = PostgresCrawlerJobRepository(async_sessionmaker(engine, expire_on_commit=False))
        job = await repo.claim_next()
        if job is None:
            raise RuntimeError("no synthetic job available")
        print(json.dumps({"phase": "claimed", "job_id": str(job.job_id),
                          "attempt_id": str(job.attempt_id)}), flush=True)
        if sys.argv[1] == "processing":
            async def synthetic_crawler(_url: str) -> dict[str, object]:
                print(json.dumps({"phase": "processing"}), flush=True)
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            await execute_crawler_job(job, repository=repo, crawler=synthetic_crawler)
        else:
            await asyncio.Event().wait()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
