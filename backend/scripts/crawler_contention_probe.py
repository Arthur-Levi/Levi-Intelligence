"""Bounded, disposable-only timing probe for the crawler claim path."""

import argparse
import asyncio
import json
import time
from collections import defaultdict
from pathlib import Path

from sqlalchemy import event, text

from scripts import crawler_capacity
from scripts.postgres_ci import test_database_url, verify_identity


def category(statement: str) -> str:
    sql = " ".join(statement.lower().split())
    if "pg_advisory_xact_lock" in sql:
        return "advisory_acquire"
    if "from crawler_jobs" in sql and "for update skip locked" in sql:
        return "recovery" if "lease_expires_at <=" in sql else "selection"
    if "from crawler_jobs" in sql and "count(" in sql:
        return "tenant_count" if "tenant_id =" in sql else "global_count"
    if "into crawler_job_attempts" in sql:
        return "attempt_insert"
    if "update crawler_jobs" in sql:
        return "job_update"
    return "other"


def summary(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "total_ms": round(sum(ordered), 3),
        "p50_ms": round(ordered[(len(ordered) - 1) // 2], 3),
        "p95_ms": round(ordered[int((len(ordered) - 1) * 0.95)], 3),
        "p99_ms": round(ordered[int((len(ordered) - 1) * 0.99)], 3),
        "max_ms": round(ordered[-1], 3),
    }


async def run(workers: int, output: Path) -> None:
    url = test_database_url()
    await verify_identity(url)
    measurements: dict[str, list[float]] = defaultdict(list)
    plans: dict[str, list[str]] = {}
    create_engine = crawler_capacity.create_async_engine

    def instrumented_engine(*args, **kwargs):
        engine = create_engine(*args, **kwargs)

        @event.listens_for(engine.sync_engine, "begin")
        def begin(connection):
            connection.info["probe_transaction_start"] = time.perf_counter()
            connection.info.pop("probe_lock_start", None)

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def before(connection, cursor, statement, parameters, context, executemany):
            context._probe_start = time.perf_counter()
            context._probe_category = category(statement)

        @event.listens_for(engine.sync_engine, "after_cursor_execute")
        def after(connection, cursor, statement, parameters, context, executemany):
            kind = context._probe_category
            in_claim = kind == "advisory_acquire" or "probe_lock_start" in connection.info
            if kind != "other" and in_claim and len(measurements[kind]) < 20000:
                measurements[kind].append((time.perf_counter() - context._probe_start) * 1000)
            if kind == "advisory_acquire":
                connection.info["probe_lock_start"] = time.perf_counter()

        def finish(connection):
            lock_start = connection.info.pop("probe_lock_start", None)
            transaction_start = connection.info.pop("probe_transaction_start", None)
            if lock_start is not None:
                now = time.perf_counter()
                measurements["lock_hold_pre_commit"].append((now - lock_start) * 1000)
                if transaction_start is not None:
                    measurements["claim_transaction_pre_commit"].append(
                        (now - transaction_start) * 1000
                    )

        event.listen(engine.sync_engine, "commit", finish)
        event.listen(engine.sync_engine, "rollback", finish)
        return engine

    async def fixture_ready(engine):
        # EXPLAIN returns plans and aggregate timings only; no synthetic row data.
        queries = {
            "recovery": (
                "SELECT * FROM crawler_jobs WHERE status = 'RUNNING' "
                "AND lease_expires_at <= now() FOR UPDATE SKIP LOCKED LIMIT 32"
            ),
            "selection": (
                "SELECT * FROM crawler_jobs WHERE status = 'QUEUED' "
                "AND cancel_requested_at IS NULL "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= now()) "
                "AND attempts < 5 ORDER BY created_at, tenant_id "
                "FOR UPDATE SKIP LOCKED LIMIT 64"
            ),
            "global_count": (
                "SELECT count(*) FROM crawler_jobs WHERE status = 'RUNNING' "
                "AND lease_expires_at > now()"
            ),
            "tenant_count": (
                "SELECT count(*) FROM crawler_jobs WHERE tenant_id = "
                "(SELECT tenant_id FROM crawler_jobs ORDER BY created_at LIMIT 1) "
                "AND status = 'RUNNING' AND lease_expires_at > now()"
            ),
        }
        async with engine.begin() as connection:
            await connection.execute(text("ANALYZE crawler_jobs"))
            for name, sql in queries.items():
                rows = (
                    await connection.execute(text("EXPLAIN (ANALYZE, BUFFERS) " + sql))
                ).scalars()
                plans[name] = list(rows)

    crawler_capacity.create_async_engine = instrumented_engine
    try:
        report = await crawler_capacity.benchmark(1000, workers, 60, fixture_ready)
    finally:
        crawler_capacity.create_async_engine = create_engine
    evidence = {
        "scope": "disposable PostgreSQL 16; 1000 synthetic completion jobs; one directed run",
        "workers": workers,
        "commit": report["commit"],
        "method": (
            "SQLAlchemy cursor execution wall time, transaction begin to commit request, "
            "and advisory acquisition completion to commit request; excludes commit "
            "round trip and pool checkout"
        ),
        "timings": {key: summary(values) for key, values in sorted(measurements.items())},
        "explain_analyze_buffers": plans,
        "benchmark": report,
    }
    output.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({
        "workers": workers,
        "timings": evidence["timings"],
        "integrity": report["integrity"],
        "aborted": report["aborted"],
    }, indent=2))
    if report["aborted"] or report["jobs_completed"] != 1000:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, choices=[2, 4, 8], required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    asyncio.run(run(arguments.workers, arguments.output))
