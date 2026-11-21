"""Effective crawler pool, PostgreSQL 16 timeout and rollback evidence."""

import asyncio
import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_crawler_db_failures_r3 import interrupt_at
from test_postgres_crawler_lifecycle import add_jobs
from test_postgres_crawler_lifecycle import world as shared_world  # noqa: F401

from app.crawler_jobs.db_resources import CrawlerDatabaseResources, create_crawler_engine
from app.crawler_jobs.postgres import CrawlerDatabaseError, PostgresCrawlerJobRepository
from app.crawler_jobs.process_executor import ProcessCrawler
from app.crawler_jobs.timing import CrawlerTiming
from app.crawler_jobs.worker import execute_crawler_job
from app.models.prospecting import CrawlerJob, CrawlerJobStatus

pytestmark = pytest.mark.integration
PROCESS_FIXTURE = Path(__file__).resolve().parents[1] / "crawler_process_fixture.py"


def fast_resources() -> CrawlerDatabaseResources:
    return replace(
        CrawlerDatabaseResources(), pool_timeout_seconds=0.2,
        connect_timeout_seconds=0.5, lock_timeout_seconds=0.1,
        statement_timeout_seconds=0.2, idle_transaction_timeout_seconds=0.2,
        command_timeout_seconds=0.5, transaction_timeout_seconds=1,
    )


async def activity_count(monitor, name: str) -> int:
    async with monitor.connect() as conn:
        return int((await conn.scalar(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
            "AND application_name=:name"
        ), {"name": name})) or 0)


@pytest.mark.asyncio
async def test_effective_pool_limit_checkout_and_disposal() -> None:
    worker_id = uuid4()
    name = f"levi_crawler_{worker_id.hex[:12]}"
    resources = replace(CrawlerDatabaseResources(), pool_timeout_seconds=0.2)
    engine = create_crawler_engine(os.environ["TEST_DATABASE_URL"], resources, worker_id)
    monitor = create_async_engine(os.environ["TEST_DATABASE_URL"], pool_size=1, max_overflow=0)
    factory = async_sessionmaker(engine)
    try:
        pool = engine.sync_engine.pool
        assert pool.size() == 2 and pool._max_overflow == 0 and pool._timeout == 0.2
        async with factory() as first, factory() as second:
            await first.execute(text("SELECT 1"))
            await second.execute(text("SELECT 1"))
            assert await activity_count(monitor, name) == 2
            started = time.monotonic()
            with pytest.raises(CrawlerDatabaseError):
                await PostgresCrawlerJobRepository(factory).claim_next()
            elapsed = time.monotonic() - started
            assert 0.15 <= elapsed < 1
            assert await activity_count(monitor, name) == 2
        assert await activity_count(monitor, name) == 2  # idle pool, not leaked checkout
    finally:
        await engine.dispose()
        assert await activity_count(monitor, name) == 0
        await monitor.dispose()


@pytest.mark.asyncio
async def test_server_settings_statement_timeout_and_connection_reuse() -> None:
    engine = create_crawler_engine(os.environ["TEST_DATABASE_URL"], fast_resources(), uuid4())
    try:
        async with engine.connect() as conn:
            settings = (await conn.execute(text(
                "SELECT current_setting('statement_timeout'), current_setting('lock_timeout'), "
                "current_setting('idle_in_transaction_session_timeout')"
            ))).one()
            assert settings == ("200ms", "100ms", "200ms")
            started = time.monotonic()
            with pytest.raises(DBAPIError):
                await conn.execute(text("SELECT pg_sleep(1)"))
            assert time.monotonic() - started < 1
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT 1")) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_row_and_advisory_lock_waits_are_bounded(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    engine = create_crawler_engine(os.environ["TEST_DATABASE_URL"], fast_resources(), uuid4())
    bounded = async_sessionmaker(engine)
    try:
        async with factory.begin() as holder:
            await holder.execute(
                select(CrawlerJob).where(CrawlerJob.id == ids[0]).with_for_update()
            )
            async with bounded() as waiter:
                started = time.monotonic()
                with pytest.raises(DBAPIError):
                    await waiter.execute(select(CrawlerJob).where(
                        CrawlerJob.id == ids[0]
                    ).with_for_update())
                assert time.monotonic() - started < 1
        async with factory.begin() as holder:
            await holder.execute(text("SELECT pg_advisory_xact_lock(681204117)"))
            started = time.monotonic()
            with pytest.raises(CrawlerDatabaseError):
                await PostgresCrawlerJobRepository(bounded).claim_next(tenants[0])
            assert time.monotonic() - started < 1
        async with bounded() as session:
            assert await session.scalar(text("SELECT 1")) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_idle_transaction_and_whole_transaction_budget() -> None:
    engine = create_crawler_engine(os.environ["TEST_DATABASE_URL"], fast_resources(), uuid4())
    factory = async_sessionmaker(engine)
    try:
        with pytest.raises(DBAPIError):
            async with factory.begin() as session:
                await session.execute(text("SELECT 1"))
                await asyncio.sleep(0.4)
                await session.execute(text("SELECT 1"))
        async with factory.begin() as holder:
            await holder.execute(text("SELECT pg_advisory_xact_lock(681204117)"))
            repo = PostgresCrawlerJobRepository(factory, transaction_timeout_seconds=0.05)
            started = time.monotonic()
            with pytest.raises(CrawlerDatabaseError):
                await repo.claim_next()
            assert time.monotonic() - started < 0.5
        async with factory() as session:
            assert await session.scalar(text("SELECT 1")) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_workers_keep_individual_pool_caps_and_tenant_active_limit(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    await add_jobs(factory, tenants[0], users[0], companies[0], 3)
    first_id, second_id = uuid4(), uuid4()
    resources = CrawlerDatabaseResources()
    first = create_crawler_engine(os.environ["TEST_DATABASE_URL"], resources, first_id)
    second = create_crawler_engine(os.environ["TEST_DATABASE_URL"], resources, second_id)
    monitor = create_async_engine(os.environ["TEST_DATABASE_URL"], pool_size=1, max_overflow=0)
    first_factory, second_factory = async_sessionmaker(first), async_sessionmaker(second)
    try:
        async with first_factory() as first_held, second_factory() as second_held:
            await first_held.execute(text("SELECT 1"))
            await second_held.execute(text("SELECT 1"))
            claims = await asyncio.gather(
                PostgresCrawlerJobRepository(first_factory).claim_next(
                    tenants[0], worker_id=first_id
                ),
                PostgresCrawlerJobRepository(second_factory).claim_next(
                    tenants[0], worker_id=second_id
                ),
            )
            assert all(claims) and len({claim.job_id for claim in claims if claim}) == 2
            assert await activity_count(monitor, f"levi_crawler_{first_id.hex[:12]}") <= 2
            assert await activity_count(monitor, f"levi_crawler_{second_id.hex[:12]}") <= 2
            assert await PostgresCrawlerJobRepository(first_factory).claim_next(tenants[0]) is None
        assert first.sync_engine.pool.size() == second.sync_engine.pool.size() == 2
        assert first.sync_engine.pool._max_overflow == second.sync_engine.pool._max_overflow == 0
    finally:
        await first.dispose()
        await second.dispose()
        assert await activity_count(monitor, f"levi_crawler_{first_id.hex[:12]}") == 0
        assert await activity_count(monitor, f"levi_crawler_{second_id.hex[:12]}") == 0
        await monitor.dispose()


@pytest.mark.asyncio
async def test_blocked_heartbeat_cannot_delay_independent_child_deadline(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, ids[0], with_for_update=True)
        row.website_url = "https://example.test/block"
    resources = replace(
        CrawlerDatabaseResources(), lock_timeout_seconds=2,
        statement_timeout_seconds=3, command_timeout_seconds=4,
        transaction_timeout_seconds=6,
    )
    engine = create_crawler_engine(os.environ["TEST_DATABASE_URL"], resources, uuid4())
    bounded = async_sessionmaker(engine)
    repo = PostgresCrawlerJobRepository(bounded, transaction_timeout_seconds=6)
    heartbeat_started = asyncio.Event()

    def observe_heartbeat(_conn, _cursor, statement, _params, _context, _many):
        if "FROM crawler_jobs" in statement and "FOR UPDATE" in statement:
            heartbeat_started.set()

    try:
        claim = await repo.claim_next(tenants[0])
        assert claim is not None and claim.attempt_id is not None
        acquired = time.monotonic()
        crawler = ProcessCrawler(
            attempt_id=claim.attempt_id, deadline=acquired + 5.4,
            termination_seconds=0.5, executor_path=PROCESS_FIXTURE,
        )
        event.listen(engine.sync_engine, "before_cursor_execute", observe_heartbeat)
        async with factory.begin() as holder:
            await holder.execute(select(CrawlerJob).where(
                CrawlerJob.id == ids[0]
            ).with_for_update())
            task = asyncio.create_task(execute_crawler_job(
                claim, repository=repo, crawler=crawler,
                timing=CrawlerTiming(attempt_seconds=5.4), acquired_at=acquired,
            ))
            await asyncio.wait_for(heartbeat_started.wait(), 7)
            assert crawler.child_pid is not None
            child_stat = Path(f"/proc/{crawler.child_pid}/stat")
            async with asyncio.timeout(2):
                while child_stat.exists() and child_stat.read_text().split()[2] != "Z":
                    await asyncio.sleep(0.01)
            assert not task.done()  # PostgreSQL row lock is still held.
            with pytest.raises(CrawlerDatabaseError):
                await asyncio.wait_for(task, 3)
        event.remove(engine.sync_engine, "before_cursor_execute", observe_heartbeat)
        async with factory() as session:
            row = await session.get(CrawlerJob, ids[0])
            assert row.status == CrawlerJobStatus.RUNNING and row.result is None
        async with factory.begin() as session:
            row = await session.get(CrawlerJob, ids[0], with_for_update=True)
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await repo.claim_next(tenants[0]) is None
        async with factory() as session:
            row = await session.get(CrawlerJob, ids[0])
            assert row.status == CrawlerJobStatus.QUEUED and row.attempts == 1
    finally:
        if event.contains(engine.sync_engine, "before_cursor_execute", observe_heartbeat):
            event.remove(engine.sync_engine, "before_cursor_execute", observe_heartbeat)
        await engine.dispose()


@pytest.mark.asyncio
async def test_heartbeat_and_cancellation_read_connection_loss_leave_lease(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    repo = PostgresCrawlerJobRepository(factory)
    claim = await repo.claim_next(tenants[0])
    assert claim is not None
    await interrupt_at(
        factory, lambda sql: "FROM crawler_jobs" in sql and "FOR UPDATE" in sql,
        lambda: repo.heartbeat(claim),
    )
    await interrupt_at(
        factory, lambda sql: "cancel_requested_at IS NOT NULL" in sql,
        lambda: repo.cancellation_requested(claim.job_id, claim.tenant_id),
    )
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        assert row.status == CrawlerJobStatus.RUNNING
        assert row.lease_token == claim.lease_token and row.result is None


@pytest.mark.asyncio
async def test_failure_persistence_connection_loss_is_not_terminal_success(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    repo = PostgresCrawlerJobRepository(factory)
    claim = await repo.claim_next(tenants[0])
    assert claim is not None
    await interrupt_at(
        factory, lambda sql: "UPDATE crawler_jobs" in sql,
        lambda: repo.persist_failure(claim, "attempt_timeout"),
    )
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        assert row.status == CrawlerJobStatus.RUNNING
        assert row.failure_code is None and row.result is None
