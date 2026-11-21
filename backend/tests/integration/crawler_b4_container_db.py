"""Synthetic rows for a bounded, actual worker-container recovery probe."""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.identity import Tenant, User
from app.models.prospecting import Company, CrawlerJob, CrawlerJobAttempt, CrawlerJobStatus
from scripts.postgres_ci import test_database_url, verify_identity

TENANT = UUID("00000000-0000-4000-8000-00000000b441")
USER = UUID("00000000-0000-4000-8000-00000000b442")
COMPANY = UUID("00000000-0000-4000-8000-00000000b443")
JOB = UUID("00000000-0000-4000-8000-00000000b444")
TOKEN = UUID("00000000-0000-4000-8000-00000000b445")


def filler_id(number: int) -> UUID:
    return uuid5(TENANT, f"b4-filler-{number}")


async def main(action: str) -> None:
    url = test_database_url()
    await verify_identity(url)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if action == "seed":
            async with factory.begin() as session:
                session.add(Tenant(id=TENANT, name="B4 synthetic", slug="b4-synthetic"))
                session.add(User(id=USER, email="b4@example.test", full_name="Synthetic user",
                                 password_hash="synthetic"))
                session.add(Company(id=COMPANY, tenant_id=TENANT, name="Synthetic company",
                                    normalized_domain="b4.example.test",
                                    website_url="https://example.test"))
                for number in range(100):
                    session.add(CrawlerJob(
                        id=filler_id(number), tenant_id=TENANT, company_id=COMPANY,
                        created_by_user_id=USER, website_url="https://example.test",
                        idempotency_key=f"b4-filler-{number}", request_fingerprint="b" * 64,
                        status=CrawlerJobStatus.QUEUED,
                        next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
                    ))
                session.add(CrawlerJob(
                    id=JOB, tenant_id=TENANT, company_id=COMPANY, created_by_user_id=USER,
                    website_url="https://example.test", idempotency_key="b4-expired",
                    request_fingerprint="b" * 64, status=CrawlerJobStatus.RUNNING,
                    attempts=1, started_at=datetime.now(UTC) - timedelta(minutes=2),
                    lease_token=TOKEN,
                    lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                ))
                session.add(CrawlerJobAttempt(
                    tenant_id=TENANT, job_id=JOB, generation=1, worker_id=USER,
                    claimed_at=datetime.now(UTC) - timedelta(minutes=2),
                ))
        elif action == "release":
            async with factory.begin() as session:
                await session.execute(delete(CrawlerJob).where(CrawlerJob.id == filler_id(0)))
        elif action == "inspect":
            async with factory() as session:
                row = await session.get(CrawlerJob, JOB)
                if row is None:
                    raise ValueError("B4 test job is absent")
                ledger = (await session.scalars(select(CrawlerJobAttempt).where(
                    CrawlerJobAttempt.job_id == JOB,
                ).order_by(CrawlerJobAttempt.generation))).all()
                queued = await session.scalar(select(func.count()).select_from(CrawlerJob).where(
                    CrawlerJob.tenant_id == TENANT,
                    CrawlerJob.status == CrawlerJobStatus.QUEUED,
                ))
                print(json.dumps({
                    "status": row.status.value,
                    "queued": queued,
                    "attempts": row.attempts,
                    "token_cleared": row.lease_token is None,
                    "ledger_generations": [entry.generation for entry in ledger],
                    "ledger_outcomes": [entry.outcome for entry in ledger],
                    "ledger_closed": [entry.ended_at is not None for entry in ledger],
                }))
        elif action == "clean":
            async with factory.begin() as session:
                await session.execute(delete(CrawlerJob).where(CrawlerJob.tenant_id == TENANT))
                await session.execute(delete(Company).where(Company.id == COMPANY))
                await session.execute(delete(User).where(User.id == USER))
                await session.execute(delete(Tenant).where(Tenant.id == TENANT))
        else:
            raise ValueError("unknown B4 test action")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
