"""Minimal real-PostgreSQL adapter and rollback smoke coverage."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.models.identity import Tenant, User
from app.models.prospecting import Company, CrawlerJob, CrawlerJobStatus

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_postgres_crawler_claim_and_transaction_rollback() -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    engine = create_async_engine(database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, user_id, company_id, job_id, rollback_tenant_id = (uuid4() for _ in range(5))
    try:
        async with factory.begin() as session:
            session.add(Tenant(id=tenant_id, name="CI crawler tenant", slug=f"ci-{tenant_id.hex}"))
            session.add(
                User(
                    id=user_id,
                    email=f"{user_id.hex}@example.test",
                    full_name="CI crawler user",
                    password_hash="synthetic-test-hash",
                )
            )
            session.add(
                Company(
                    id=company_id,
                    tenant_id=tenant_id,
                    name="Synthetic crawler company",
                    normalized_domain=f"{company_id.hex}.example.test",
                    website_url="https://example.test",
                )
            )
            session.add(
                CrawlerJob(
                    id=job_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    created_by_user_id=user_id,
                    website_url="https://example.test",
                    idempotency_key=f"ci-{job_id.hex}",
                    request_fingerprint="a" * 64,
                    status=CrawlerJobStatus.QUEUED,
                )
            )

        repository = PostgresCrawlerJobRepository(factory)
        claimed = await repository.claim_next()
        assert claimed is not None
        assert claimed.job_id == job_id
        assert claimed.tenant_id == tenant_id
        assert claimed.company_id == company_id
        assert claimed.lease_token is not None

        with pytest.raises(RuntimeError, match="intentional rollback check"):
            async with factory.begin() as session:
                session.add(
                    Tenant(
                        id=rollback_tenant_id,
                        name="Rollback-only tenant",
                        slug=f"rollback-{rollback_tenant_id.hex}",
                    )
                )
                await session.flush()
                raise RuntimeError("intentional rollback check")

        async with factory() as session:
            rolled_back = await session.scalar(
                select(Tenant.id).where(Tenant.id == rollback_tenant_id)
            )
        assert rolled_back is None
    finally:
        async with factory.begin() as session:
            await session.execute(delete(CrawlerJob).where(CrawlerJob.id == job_id))
            await session.execute(delete(Company).where(Company.id == company_id))
            await session.execute(delete(User).where(User.id == user_id))
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await session.execute(delete(Tenant).where(Tenant.id == rollback_tenant_id))
        await engine.dispose()
