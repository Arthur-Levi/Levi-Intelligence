"""Bounded synthetic crawler completion benchmark for disposable CI PostgreSQL only."""

import argparse
import asyncio
import json
import os
import platform
import resource
import statistics
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crawler_jobs.contracts import CrawlerJobResult
from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.models.identity import Membership, MembershipRole, Tenant, User
from app.models.prospecting import (
    Company,
    CrawlerJob,
    CrawlerJobAttempt,
    CrawlerJobStatus,
    SourceRecord,
)
from scripts.postgres_ci import test_database_url, verify_identity

SAMPLE_INTERVAL_S = 0.1
MAX_TELEMETRY_SAMPLES = 6000


def sampling_budget(seconds: int) -> int:
    return min(MAX_TELEMETRY_SAMPLES, max(1, int(seconds / SAMPLE_INTERVAL_S)))


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * percent))], 6)


def percentiles(values: list[float]) -> dict[str, float | None]:
    return {
        name: percentile(values, value)
        for name, value in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))
    }


def fairness(counts: list[int]) -> dict:
    total = sum(counts)
    return {
        "completed_per_worker": counts,
        "min_max_ratio": round(min(counts) / max(counts), 6) if max(counts, default=0) else None,
        "jain_index": round(total * total / (len(counts) * sum(x * x for x in counts)), 6)
        if total and counts
        else None,
    }


def aggregate_reports(reports: list[dict]) -> dict:
    """Keep failed runs visible; calculate distributions from successful runs only."""
    groups: dict[int, list[dict]] = {}
    for report in reports:
        groups.setdefault(report["workers"], []).append(report)
    result = {}
    for workers, runs in sorted(groups.items()):
        successful = [
            r
            for r in runs
            if not r.get("aborted")
            and r.get("jobs_completed") == r.get("workload")
            and all(v == "PASS" for v in r.get("integrity", {}).values() if v != "NOT TESTED")
        ]
        metrics = {}
        for key, path in (
            ("throughput_jobs_s", ("throughput_jobs_s",)),
            ("claim_p95_s", ("claim_latency_s", "p95")),
            ("claim_p99_s", ("claim_latency_s", "p99")),
            ("completion_p95_s", ("completion_latency_s", "p95")),
            ("completion_p99_s", ("completion_latency_s", "p99")),
        ):
            values = [r[path[0]][path[1]] if len(path) == 2 else r[path[0]] for r in successful]
            values = [v for v in values if v is not None]
            metrics[key] = (
                {
                    "median": statistics.median(values),
                    "min": min(values),
                    "max": max(values),
                    "range": max(values) - min(values),
                }
                if values
                else None
            )
        result[str(workers)] = {
            "runs": len(runs),
            "successful": len(successful),
            "failed_or_aborted": len(runs) - len(successful),
            "metrics": metrics,
        }
    return result


def count_wait_snapshot(rows: list[tuple]) -> tuple[Counter[str], int]:
    """Count waiting sessions in one observation, never distinct wait events."""
    counts: Counter[str] = Counter()
    waiting = 0
    for wait_type, wait_event, advisory, other in rows:
        if wait_type:
            counts[f"wait_event_type:{wait_type}"] += 1
            counts[f"wait_event:{wait_type}:{wait_event}"] += 1
        if wait_type == "Lock":
            waiting += 1
            counts["lock_wait_samples"] += 1
            counts[
                "advisory_lock_wait_samples"
                if advisory
                else "other_lock_wait_samples"
                if other
                else "unclassified_lock_wait_samples"
            ] += 1
    return counts, waiting


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
    worker_counts = [0] * workers
    sample_counts: Counter[str] = Counter()
    peak_waiting = 0
    peak_active = 0
    peak_tenant_active = 0
    telemetry_samples = 0
    claim_calls = 0
    cpu_before = resource.getrusage(resource.RUSAGE_SELF)
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
        peak_connections = 0
        stop_monitor = asyncio.Event()

        async def monitor() -> None:
            nonlocal peak_connections, peak_waiting, peak_active, peak_tenant_active
            nonlocal telemetry_samples, aborted
            # One bounded sample per 100 ms. Each query observes sessions, not lock events.
            while not stop_monitor.is_set():
                if telemetry_samples >= sampling_budget(seconds):
                    aborted = True
                    errors.append("telemetry_sample_ceiling")
                    return
                if time.monotonic() >= deadline:
                    aborted = True
                    errors.append("time_budget")
                    return
                async with factory() as session:
                    rows = (
                        await session.execute(
                            text("""
                        SELECT a.wait_event_type, a.wait_event,
                               EXISTS (SELECT 1 FROM pg_locks l WHERE l.pid = a.pid
                                       AND NOT l.granted AND l.locktype = 'advisory'),
                               EXISTS (SELECT 1 FROM pg_locks l WHERE l.pid = a.pid
                                       AND NOT l.granted AND l.locktype <> 'advisory')
                        FROM pg_stat_activity a
                        WHERE a.datname = current_database()
                    """)
                        )
                    ).all()
                    grouped = (
                        select(CrawlerJob.tenant_id, func.count().label("tenant_count"))
                        .where(
                            CrawlerJob.tenant_id.in_(tenants),
                            CrawlerJob.status == CrawlerJobStatus.RUNNING,
                            CrawlerJob.lease_expires_at > func.now(),
                        )
                        .group_by(CrawlerJob.tenant_id)
                        .subquery()
                    )
                    active = (
                        await session.execute(
                            select(
                                func.coalesce(func.sum(grouped.c.tenant_count), 0),
                                func.coalesce(func.max(grouped.c.tenant_count), 0),
                            )
                        )
                    ).one()
                telemetry_samples += 1
                peak_connections = max(peak_connections, len(rows))
                peak_active = max(peak_active, int(active[0]))
                peak_tenant_active = max(peak_tenant_active, int(active[1]))
                snapshot, waiting = count_wait_snapshot(rows)
                sample_counts.update(snapshot)
                peak_waiting = max(peak_waiting, waiting)
                if (
                    peak_connections > 12
                    or resource.getrusage(resource.RUSAGE_SELF).ru_maxrss > 512 * 1024
                ):
                    aborted = True
                    errors.append(
                        "connection_ceiling" if peak_connections > 12 else "memory_ceiling"
                    )
                    return
                try:
                    await asyncio.wait_for(stop_monitor.wait(), timeout=SAMPLE_INTERVAL_S)
                except TimeoutError:
                    pass

        async def work(index: int) -> None:
            nonlocal completed, aborted, claim_calls
            while completed < size and not aborted:
                if time.monotonic() >= deadline:
                    aborted = True
                    errors.append("time_budget")
                    return
                began = time.monotonic()
                claim = await repository.claim_next()
                claim_calls += 1
                if len(claim_latencies) < 20000:
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
                worker_counts[index] += 1

        monitor_task = asyncio.create_task(monitor())
        try:
            await asyncio.wait_for(asyncio.gather(*(work(i) for i in range(workers))), seconds + 5)
        except TimeoutError:
            aborted = True
            errors.append("time_budget")
        finally:
            stop_monitor.set()
            await monitor_task
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
            attempts = (
                await session.scalars(
                    select(CrawlerJobAttempt).where(CrawlerJobAttempt.job_id.in_(job_ids))
                )
            ).all()
            records = (
                await session.scalars(
                    select(SourceRecord).where(SourceRecord.tenant_id.in_(tenants))
                )
            ).all()
        row_by_id = {row.id: row for row in rows}
        row_by_string_id = {str(row.id): row for row in rows}
        source_ids = [record.captured_data.get("crawler_job_id") for record in records]
        integrity = {
            "all_jobs_completed": "PASS" if len(rows) == size and committed == size else "FAIL",
            "no_duplicate_results": "PASS"
            if len(source_ids) == len(set(source_ids)) == size
            else "FAIL",
            "no_double_completion": "PASS" if all(row.attempts == 1 for row in rows) else "FAIL",
            "attempt_ledger": "PASS"
            if len(attempts) == size
            and all(
                a.generation == 1
                and a.outcome == "completed"
                and a.ended_at is not None
                and a.worker_id is not None
                and row_by_id[a.job_id].tenant_id == a.tenant_id
                for a in attempts
            )
            else "FAIL",
            "lease_fencing_state": "PASS"
            if all(row.lease_token is None and row.lease_expires_at is None for row in rows)
            else "FAIL",
            "tenant_isolation": "PASS"
            if len(records) == size
            and all(
                (row := row_by_string_id.get(record.captured_data.get("crawler_job_id")))
                is not None
                and row.tenant_id == record.tenant_id
                for record in records
            )
            else "FAIL",
            "global_active_limit": "PASS"
            if peak_active <= 8 and telemetry_samples
            else "NOT TESTED",
            "tenant_active_limit": "PASS"
            if peak_tenant_active <= 2 and telemetry_samples
            else "NOT TESTED",
            "connection_ceiling": "PASS"
            if peak_connections <= 12 and telemetry_samples
            else "NOT TESTED",
            "memory_ceiling": "PASS"
            if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss <= 512 * 1024
            else "FAIL",
            "time_budget": "PASS" if elapsed <= seconds + 5 else "FAIL",
            "external_requests": "NOT TESTED",
            "admission": "NOT TESTED",
            "stale_writes": "NOT TESTED",
            "failure_recovery": "NOT TESTED",
        }
        if committed != completed or sources != committed or "FAIL" in integrity.values():
            aborted = True
            errors.append("critical_transaction_invariant")
        cpu_after = resource.getrusage(resource.RUSAGE_SELF)
        repository_root = Path(__file__).resolve().parents[2]
        commit = subprocess.check_output(
            ["git", "-c", f"safe.directory={repository_root}", "rev-parse", "HEAD"],
            text=True,
            cwd=repository_root,
        ).strip()
        return {
            "commit": commit,
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
            "claim_latency_s": percentiles(claim_latencies),
            "completion_latency_s": percentiles(completion_latencies),
            "peak_memory_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "peak_postgres_connections": peak_connections,
            "lock_wait_samples": sample_counts["lock_wait_samples"],
            "cpu_seconds": round(cpu_after.ru_utime - cpu_before.ru_utime, 3),
            "cpu_system_seconds": round(cpu_after.ru_stime - cpu_before.ru_stime, 3),
            "cpu_method": "process resource.getrusage delta; excludes PostgreSQL server CPU",
            "lock_sampling_interval_ms": int(SAMPLE_INTERVAL_S * 1000),
            "lock_sampling_count": telemetry_samples,
            "lock_wait_semantics": "sum of waiting session snapshots; not distinct events",
            "maximum_simultaneous_waiting_sessions": peak_waiting,
            "advisory_lock_wait_samples": sample_counts["advisory_lock_wait_samples"],
            "other_lock_wait_samples": sample_counts["other_lock_wait_samples"],
            "unclassified_lock_wait_samples": sample_counts["unclassified_lock_wait_samples"],
            "wait_event_samples": dict(sample_counts),
            "peak_active_jobs_sampled": peak_active,
            "peak_tenant_active_jobs_sampled": peak_tenant_active,
            "claim_calls": claim_calls,
            "claim_latency_samples": len(claim_latencies),
            "worker_fairness": fairness(worker_counts),
            "integrity": integrity,
            "python": platform.python_version(),
            "runner_cpu_count": os.cpu_count(),
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
