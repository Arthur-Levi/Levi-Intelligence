"""Fail-closed synthetic database fixture for the actual B.5 worker topology."""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from fastapi import Request
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.dependencies import TenantContext
from app.api.v1.endpoints.prospecting import cancel_crawler_job
from app.models.identity import AuditEvent, Membership, MembershipRole, Tenant, User
from app.models.prospecting import (
    Company,
    CrawlerJob,
    CrawlerJobAttempt,
    CrawlerJobStatus,
    SourceRecord,
)
from scripts.postgres_ci import test_database_url, verify_identity

NAMESPACE = UUID("00000000-0000-4000-8000-00000000b500")
TENANTS = [uuid5(NAMESPACE, f"tenant-{index}") for index in range(5)]
USERS = [uuid5(NAMESPACE, f"user-{index}") for index in range(5)]
COMPANIES = [uuid5(NAMESPACE, f"company-{index}") for index in range(5)]


def job_id(identity: str) -> UUID:
    return uuid5(NAMESPACE, f"job-{identity}")


def job(identity: str, index: int, mode: str) -> CrawlerJob:
    return CrawlerJob(
        id=job_id(identity), tenant_id=TENANTS[index], company_id=COMPANIES[index],
        created_by_user_id=USERS[index],
        website_url=f"https://synthetic.example.test/{mode}/{identity}",
        idempotency_key=f"b5-{identity}", request_fingerprint="b" * 64,
        status=CrawlerJobStatus.QUEUED,
    )


async def main(action: str, argument: str | None) -> None:
    url = test_database_url()
    await verify_identity(url)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if action == "seed-eight":
            async with factory.begin() as session:
                for index in range(5):
                    session.add(Tenant(id=TENANTS[index], name="B5 synthetic",
                                       slug=f"b5-synthetic-{index}"))
                    session.add(User(id=USERS[index], email=f"b5-{index}@example.test",
                                     full_name="Synthetic user", password_hash="synthetic"))
                    session.add(Membership(user_id=USERS[index], tenant_id=TENANTS[index],
                                           role=MembershipRole.OWNER))
                    session.add(Company(id=COMPANIES[index], tenant_id=TENANTS[index],
                                        name="Synthetic company",
                                        normalized_domain=f"b5-{index}.example.test",
                                        website_url="https://synthetic.example.test"))
                for index in range(4):
                    session.add(job(f"t{index}a", index, "hold"))
                    session.add(job(f"t{index}b", index, "hold"))
                session.add(job("t0third", 0, "finish"))
                session.add(job("t4ninth", 4, "finish"))
        elif action == "seed-cycle":
            if argument is None or not argument.isdecimal() or not 0 <= int(argument) < 10:
                raise ValueError("cycle must be 0..9")
            cycle = int(argument)
            async with factory.begin() as session:
                for number in range(10):
                    identity = f"c{cycle}-{number}"
                    session.add(job(identity, number % 5, "crash" if number % 5 == 0 else "finish"))
        elif action == "cancel":
            if argument is None or argument not in {f"t{i}{part}" for i in range(4)
                                                     for part in ("a", "b")}:
                raise ValueError("invalid hold job")
            index = int(argument[1])
            async with factory() as session:
                user = await session.get(User, USERS[index])
                tenant = await session.get(Tenant, TENANTS[index])
                membership = await session.scalar(select(Membership).where(
                    Membership.user_id == USERS[index],
                    Membership.tenant_id == TENANTS[index],
                ))
                assert user is not None and tenant is not None and membership is not None
                ctx = TenantContext(
                    user=user, tenant=tenant, membership=membership,
                )
            async with factory() as session:
                response = await cancel_crawler_job(
                    job_id(argument), Request({"type": "http", "client": ("127.0.0.1", 1)}),
                    ctx, session,
                )
            print(json.dumps({"job": argument, "status": response.status}))
        elif action == "expire":
            async with factory.begin() as session:
                rows = (await session.scalars(select(CrawlerJob).where(
                    CrawlerJob.tenant_id.in_(TENANTS),
                    CrawlerJob.status == CrawlerJobStatus.RUNNING,
                ).with_for_update())).all()
                for row in rows:
                    row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            print(json.dumps({"expired": len(rows)}))
        elif action == "stats":
            async with factory() as session:
                rows = (await session.scalars(select(CrawlerJob).where(
                    CrawlerJob.tenant_id.in_(TENANTS)
                ))).all()
                attempts = (await session.scalars(select(CrawlerJobAttempt).where(
                    CrawlerJobAttempt.tenant_id.in_(TENANTS)
                ))).all()
                sources = await session.scalar(select(func.count()).select_from(SourceRecord).where(
                    SourceRecord.tenant_id.in_(TENANTS)
                ))
                connections = await session.scalar(text("SELECT count(*) FROM pg_stat_activity"))
                worker_connections = (await session.execute(text(
                    "SELECT state, count(*) FROM pg_stat_activity "
                    "WHERE application_name LIKE 'levi_crawler_%' GROUP BY state"
                ))).all()
            now = datetime.now(UTC)
            counts = {status.value: sum(row.status == status for row in rows)
                      for status in CrawlerJobStatus}
            active = [row for row in rows if row.status == CrawlerJobStatus.RUNNING
                      and row.lease_expires_at is not None and row.lease_expires_at > now]
            print(json.dumps({
                "counts": counts, "valid_active": len(active),
                "active_by_tenant": [sum(row.tenant_id == tenant for row in active)
                                     for tenant in TENANTS],
                "attempts": len(attempts), "generations_unique": len({
                    (entry.job_id, entry.generation) for entry in attempts
                }) == len(attempts),
                "sources": sources, "connections_total": connections,
                "worker_connections": {str(state): count for state, count in worker_connections},
                "jobs": {identity: next((row.status.value for row in rows
                                         if row.id == job_id(identity)), None)
                         for identity in [*(f"t{i}{part}" for i in range(4)
                                            for part in ("a", "b")), "t0third", "t4ninth"]},
            }))
        elif action == "clean":
            async with factory.begin() as session:
                await session.execute(delete(AuditEvent).where(AuditEvent.tenant_id.in_(TENANTS)))
                await session.execute(
                    delete(SourceRecord).where(SourceRecord.tenant_id.in_(TENANTS))
                )
                await session.execute(delete(CrawlerJob).where(CrawlerJob.tenant_id.in_(TENANTS)))
                await session.execute(delete(Company).where(Company.tenant_id.in_(TENANTS)))
                await session.execute(delete(Membership).where(Membership.tenant_id.in_(TENANTS)))
                await session.execute(delete(User).where(User.id.in_(USERS)))
                await session.execute(delete(Tenant).where(Tenant.id.in_(TENANTS)))
        else:
            raise ValueError("unknown B5 fixture action")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
