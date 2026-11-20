"""Bounded synthetic crawler completion benchmark for disposable CI PostgreSQL only."""

import argparse
import asyncio
import json
import os
import platform
import resource
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crawler_jobs.contracts import CrawlerJobResult
from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.models.identity import Membership, MembershipRole, Tenant, User
from app.models.prospecting import Company, CrawlerJob, CrawlerJobStatus, SourceRecord
from scripts.postgres_ci import test_database_url, verify_identity


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * percent))], 6)


async def benchmark(size: int, workers: int, seconds: int) -> dict:
    url = test_database_url()
    await verify_identity(url)
    engine = create_async_engine(
        url, pool_size=min(workers + 2, 10), max_overflow=0, pool_timeout=5, pool_pre_ping=True
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgresCrawlerJobRepository(factory)
    tenants: list = []
    users: list = []
    job_ids: list = []
    claim_latencies: list[float] = []
    completion_latencies: list[float] = []
    errors: list[str] = []
    started = time.monotonic()
    deadline = started + seconds
    completed = 0
    aborted = False
    try:
        tenant_count = max(4, (size + 99) // 100)
        async with factory.begin() as session:
            for index in range(tenant_count):
                tenant, user, company = uuid4(), uuid4(), uuid4()
                tenants.append(tenant)
                users.append(user)
                session.add(Tenant(id=tenant, name="Capacity synthetic", slug=f"cap-{tenant.hex}"))
                session.add(
                    User(
                        id=user,
                        email=f"{user.hex}@example.test",
                        full_name="Capacity synthetic",
                        password_hash="synthetic",
                    )
                )
                session.add(Membership(user_id=user, tenant_id=tenant, role=MembershipRole.OWNER))
                session.add(
                    Company(
                        id=company,
                        tenant_id=tenant,
                        name="Capacity synthetic",
                        normalized_domain=f"{company.hex}.example.test",
                        website_url="https://example.test",
                    )
                )
                allocation = min(
                    100, (size - len(job_ids) + tenant_count - index - 1) // (tenant_count - index)
                )
                for _ in range(allocation):
                    job_id = uuid4()
                    job_ids.append(job_id)
                    session.add(
                        CrawlerJob(
                            id=job_id,
                            tenant_id=tenant,
                            company_id=company,
                            created_by_user_id=user,
                            website_url="https://example.test",
                            idempotency_key=job_id.hex,
                            request_fingerprint="0" * 64,
                        )
                    )
        async with factory() as session:
            peak_connections = int(
                await session.scalar(
                    text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
                )
                or 0
            )
        lock_waits = 0

        async def work() -> None:
            nonlocal completed, peak_connections, lock_waits, aborted
            while completed < size and not aborted:
                if time.monotonic() >= deadline:
                    aborted = True
                    errors.append("time_budget")
                    return
                began = time.monotonic()
                claim = await repository.claim_next()
                claim_latencies.append(time.monotonic() - began)
                if claim is None:
                    await asyncio.sleep(0.01)
                    continue
                began = time.monotonic()
                await repository.persist_result(
                    claim,
                    CrawlerJobResult(
                        status="verified", status_code=200, checked_at=datetime.now(UTC)
                    ),
                )
                completion_latencies.append(time.monotonic() - began)
                completed += 1
                if completed % 25 == 0:
                    async with factory() as session:
                        peak_connections = max(
                            peak_connections,
                            int(
                                await session.scalar(
                                    text(
                                        "SELECT count(*) FROM pg_stat_activity "
                                        "WHERE datname = current_database()"
                                    )
                                )
                                or 0
                            ),
                        )
                        lock_waits += int(
                            await session.scalar(
                                text(
                                    "SELECT count(*) FROM pg_stat_activity "
                                    "WHERE datname = current_database() "
                                    "AND wait_event_type = 'Lock'"
                                )
                            )
                            or 0
                        )
                if peak_connections > 12:
                    aborted = True
                    errors.append("connection_ceiling")
                    return
                if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss > 512 * 1024:
                    aborted = True
                    errors.append("memory_ceiling")
                    return

        try:
            await asyncio.wait_for(asyncio.gather(*(work() for _ in range(workers))), seconds + 5)
        except TimeoutError:
            aborted = True
            errors.append("time_budget")
        elapsed = time.monotonic() - started
        async with factory() as session:
            rows = (
                await session.scalars(select(CrawlerJob).where(CrawlerJob.id.in_(job_ids)))
            ).all()
            committed = sum(row.status == CrawlerJobStatus.COMPLETED for row in rows)
            sources = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SourceRecord)
                    .where(SourceRecord.tenant_id.in_(tenants))
                )
                or 0
            )
        if committed != completed or sources != committed:
            aborted = True
            errors.append("critical_transaction_invariant")
        return {
            "commit": os.environ.get("GITHUB_SHA", "local-unverified"),
            "postgresql": "16 (identity preflight)",
            "runner": platform.platform(),
            "workload": size,
            "workers": workers,
            "concurrency": workers,
            "duration_s": round(elapsed, 3),
            "jobs_admitted": None,
            "jobs_rejected": None,
            "jobs_claimed": len(completion_latencies),
            "jobs_completed": committed,
            "jobs_failed": 0,
            "jobs_cancelled": 0,
            "duplicate_results": max(0, sources - committed),
            "stale_writes": None,
            "throughput_jobs_s": round(committed / elapsed, 3),
            "admission_latency_s": None,
            "claim_latency_s": {
                p: percentile(claim_latencies, v)
                for p, v in {"p50": 0.5, "p95": 0.95, "p99": 0.99}.items()
            },
            "completion_latency_s": {
                p: percentile(completion_latencies, v)
                for p, v in {"p50": 0.5, "p95": 0.95, "p99": 0.99}.items()
            },
            "peak_memory_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "peak_postgres_connections": peak_connections,
            "lock_wait_samples": lock_waits,
            "cpu_seconds": round(resource.getrusage(resource.RUSAGE_SELF).ru_utime, 3),
            "aborted": aborted,
            "failure_classifications": errors,
            "fixture": "direct synthetic insert; admission API not measured",
            "time_budget_s": seconds,
            "memory_ceiling_kib": 512 * 1024,
            "connection_ceiling": 12,
        }
    finally:
        if tenants:
            async with factory.begin() as session:
                await session.execute(
                    delete(SourceRecord).where(SourceRecord.tenant_id.in_(tenants))
                )
                await session.execute(delete(CrawlerJob).where(CrawlerJob.tenant_id.in_(tenants)))
                await session.execute(delete(Company).where(Company.tenant_id.in_(tenants)))
                await session.execute(delete(Membership).where(Membership.tenant_id.in_(tenants)))
                await session.execute(delete(User).where(User.id.in_(users)))
                await session.execute(delete(Tenant).where(Tenant.id.in_(tenants)))
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, choices=[100, 500, 1000], default=100)
    parser.add_argument("--workers", type=int, choices=[1, 2, 4, 8], default=2)
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--output", type=Path, default=Path("capacity-report.json"))
    args = parser.parse_args()
    if not 30 <= args.seconds <= 600:
        parser.error("seconds must be between 30 and 600")
    report = asyncio.run(benchmark(args.size, args.workers, args.seconds))
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if report["aborted"] or report["jobs_completed"] != args.size:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
