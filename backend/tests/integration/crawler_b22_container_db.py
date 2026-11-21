"""Test-only PostgreSQL seed and inspection for the actual crawler container."""

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.models.identity import Tenant, User
from app.models.prospecting import (
    Company,
    CrawlerJob,
    CrawlerJobAttempt,
    CrawlerJobStatus,
    SourceRecord,
)

TENANT = UUID("00000000-0000-4000-8000-00000000b221")
USER = UUID("00000000-0000-4000-8000-00000000b222")
COMPANY = UUID("00000000-0000-4000-8000-00000000b223")
JOB = UUID("00000000-0000-4000-8000-00000000b224")


async def main(action: str) -> None:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if action == "seed":
            async with factory.begin() as session:
                session.add(Tenant(id=TENANT, name="B22 synthetic", slug="b22-synthetic"))
                session.add(User(
                    id=USER, email="b22@example.test", full_name="Synthetic user",
                    password_hash="synthetic",
                ))
                session.add(Company(
                    id=COMPANY, tenant_id=TENANT, name="Synthetic company",
                    normalized_domain="b22.example.test", website_url="https://example.test",
                ))
                session.add(CrawlerJob(
                    id=JOB, tenant_id=TENANT, company_id=COMPANY, created_by_user_id=USER,
                    website_url="https://example.test", idempotency_key="b22-synthetic-job",
                    request_fingerprint="b" * 64, status=CrawlerJobStatus.QUEUED,
                ))
        elif action == "expire":
            async with factory.begin() as session:
                row = await session.get(CrawlerJob, JOB, with_for_update=True)
                row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            repo = PostgresCrawlerJobRepository(factory)
            await repo.claim_next(TENANT)
        elif action == "release":
            async with factory.begin() as session:
                row = await session.get(CrawlerJob, JOB, with_for_update=True)
                row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        elif action == "clean":
            async with factory.begin() as session:
                await session.execute(
                    delete(SourceRecord).where(SourceRecord.company_id == COMPANY)
                )
                await session.execute(delete(CrawlerJob).where(CrawlerJob.id == JOB))
                await session.execute(delete(Company).where(Company.id == COMPANY))
                await session.execute(delete(User).where(User.id == USER))
                await session.execute(delete(Tenant).where(Tenant.id == TENANT))
        elif action == "inspect":
            async with factory() as session:
                row = await session.get(CrawlerJob, JOB)
                attempts = (await session.scalars(select(CrawlerJobAttempt).where(
                    CrawlerJobAttempt.job_id == JOB
                ).order_by(CrawlerJobAttempt.generation))).all()
                sources = await session.scalar(select(func.count()).select_from(SourceRecord).where(
                    SourceRecord.company_id == COMPANY
                ))
                print({
                    "status": row.status.value, "attempts": row.attempts,
                    "outcomes": [attempt.outcome for attempt in attempts],
                    "sources": sources,
                })
        else:
            raise ValueError("unknown test action")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
