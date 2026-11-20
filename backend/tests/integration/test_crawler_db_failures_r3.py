"""Bounded real PostgreSQL connection interruption and pool checkout tests."""

import asyncio
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from crawler_faults import FaultPlan, FaultPoint, FaultRepository, InjectedFault
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_postgres_crawler_lifecycle import add_jobs, result
from test_postgres_crawler_lifecycle import world as shared_world  # noqa: F401

from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.models.prospecting import CrawlerJob, CrawlerJobAttempt, CrawlerJobStatus, SourceRecord

pytestmark = pytest.mark.integration


async def interrupt_at(factory, predicate: Callable[[str], bool], operation):
    """Terminate the actual asyncpg socket immediately before a selected SQL send."""
    engine = factory.kw["bind"]
    fired = False

    def terminate(connection, _cursor, statement, _params, _context, _many):
        nonlocal fired
        if not fired and predicate(statement):
            fired = True
            connection.connection.driver_connection.terminate()

    event.listen(engine.sync_engine, "before_cursor_execute", terminate)
    try:
        with pytest.raises(DBAPIError):
            await asyncio.wait_for(operation(), 5)
        assert fired
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", terminate)


@pytest.mark.asyncio
async def test_connection_lost_before_claim_transaction_then_restored(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    repo = PostgresCrawlerJobRepository(factory)
    await interrupt_at(factory, lambda sql: "pg_advisory_xact_lock" in sql,
                       lambda: repo.claim_next(tenants[0]))
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        assert row.status == CrawlerJobStatus.QUEUED and row.attempts == 0
    claim = await asyncio.wait_for(repo.claim_next(tenants[0]), 5)
    assert claim is not None


@pytest.mark.asyncio
async def test_connection_lost_during_claim_rolls_back_attempt(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    repo = PostgresCrawlerJobRepository(factory)
    await interrupt_at(factory, lambda sql: "INSERT INTO crawler_job_attempts" in sql,
                       lambda: repo.claim_next(tenants[0]))
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        attempts = await session.scalar(select(func.count()).select_from(CrawlerJobAttempt).where(
            CrawlerJobAttempt.job_id == ids[0]))
    assert row.status == CrawlerJobStatus.QUEUED and row.attempts == 0
    assert attempts == 0
    assert await repo.claim_next(tenants[0]) is not None


@pytest.mark.asyncio
async def test_connection_lost_during_result_rolls_back_all_rows(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    repo = PostgresCrawlerJobRepository(factory)
    claim = await repo.claim_next(tenants[0])
    assert claim is not None
    await interrupt_at(factory, lambda sql: "INSERT INTO source_records" in sql,
                       lambda: repo.persist_result(claim, result()))
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        source_count = await session.scalar(select(func.count()).select_from(SourceRecord).where(
            SourceRecord.tenant_id == tenants[0]))
    assert row.status == CrawlerJobStatus.RUNNING and source_count == 0
    await repo.persist_result(claim, result())
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        assert row.status == CrawlerJobStatus.COMPLETED


@pytest.mark.asyncio
async def test_connection_lost_during_retry_preserves_attempt(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    repo = PostgresCrawlerJobRepository(factory)
    claim = await repo.claim_next(tenants[0])
    assert claim is not None
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, ids[0], with_for_update=True)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await interrupt_at(factory, lambda sql: "UPDATE crawler_jobs" in sql,
                       lambda: repo.claim_next(tenants[0]))
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        attempt = await session.get(CrawlerJobAttempt, claim.attempt_id)
    assert row.status == CrawlerJobStatus.RUNNING
    assert attempt.ended_at is None
    assert await repo.claim_next(tenants[0]) is None


@pytest.mark.asyncio
async def test_bounded_pool_checkout_timeout_then_recovery(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    await add_jobs(factory, tenants[0], users[0], companies[0])
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"], pool_size=1,
                                 max_overflow=0, pool_timeout=0.2, pool_pre_ping=True)
    tiny_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with tiny_factory() as holder:
            await holder.execute(text("SELECT 1"))
            repo = PostgresCrawlerJobRepository(tiny_factory)
            with pytest.raises(PoolTimeout):
                await asyncio.wait_for(repo.claim_next(tenants[0]), 2)
        assert await asyncio.wait_for(repo.claim_next(tenants[0]), 5) is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_database_temporarily_unavailable_then_restored(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    maintenance_url = make_url(os.environ["TEST_DATABASE_URL"]).set(database="postgres")
    maintenance = create_async_engine(maintenance_url, isolation_level="AUTOCOMMIT",
                                      pool_size=1, max_overflow=0)
    fresh = create_async_engine(os.environ["TEST_DATABASE_URL"], pool_size=1,
                                max_overflow=0, connect_args={"timeout": 1})
    try:
        async with maintenance.connect() as control:
            await control.execute(text(
                "ALTER DATABASE levi_gate_1b1_ci WITH ALLOW_CONNECTIONS false"))
        try:
            with pytest.raises((OSError, DBAPIError)):
                await asyncio.wait_for(
                    PostgresCrawlerJobRepository(async_sessionmaker(fresh)).claim_next(), 3)
        finally:
            async with maintenance.connect() as control:
                await control.execute(text(
                    "ALTER DATABASE levi_gate_1b1_ci WITH ALLOW_CONNECTIONS true"))
    finally:
        await fresh.dispose()
        await maintenance.dispose()
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        assert row.status == CrawlerJobStatus.QUEUED
    assert await asyncio.wait_for(PostgresCrawlerJobRepository(factory).claim_next(tenants[0]), 5)


@pytest.mark.asyncio
async def test_ambiguous_commit_reconciles_authoritative_state(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    claim = await PostgresCrawlerJobRepository(factory).claim_next(tenants[0])
    assert claim is not None
    plan = FaultPlan(FaultPoint.AMBIGUOUS_RESULT_COMMIT)
    with pytest.raises(InjectedFault):
        await FaultRepository(factory, plan).persist_result(claim, result())
    plan.verify()
    # Do not retry a non-idempotent completion after ambiguous acknowledgment.
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        attempts = (await session.scalars(select(CrawlerJobAttempt).where(
            CrawlerJobAttempt.job_id == ids[0]))).all()
        source_count = await session.scalar(select(func.count()).select_from(SourceRecord).where(
            SourceRecord.tenant_id == tenants[0]))
    assert row.status == CrawlerJobStatus.COMPLETED
    assert len(attempts) == 1 and attempts[0].outcome == "completed"
    assert source_count == 1
