"""Real process death and PostgreSQL lease recovery evidence."""

import asyncio
import json
import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_postgres_crawler_lifecycle import add_jobs, context, request, result
from test_postgres_crawler_lifecycle import world as shared_world  # noqa: F401

from app.api.v1.endpoints.prospecting import cancel_crawler_job
from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.models.prospecting import CrawlerJob, CrawlerJobAttempt, CrawlerJobStatus, SourceRecord

pytestmark = pytest.mark.integration
CHILD = Path(__file__).with_name("crawler_crash_child.py")


async def kill_after_phase(phase: str) -> tuple[str, str]:
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    child = await asyncio.create_subprocess_exec(
        sys.executable, str(CHILD), phase,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[2], env=child_env,
    )
    try:
        assert child.stdout is not None
        first_line = await asyncio.wait_for(child.stdout.readline(), 8)
        assert first_line, f"child exited before claim (code={await child.wait()})"
        claimed = json.loads(first_line)
        assert claimed["phase"] == "claimed"
        if phase == "processing":
            processing = json.loads(await asyncio.wait_for(child.stdout.readline(), 8))
            assert processing["phase"] == "processing"
        assert child.returncode is None
        child.send_signal(signal.SIGKILL)
        assert await asyncio.wait_for(child.wait(), 5) == -signal.SIGKILL
        return claimed["job_id"], claimed["attempt_id"]
    finally:
        if child.returncode is None:
            child.kill()
            await asyncio.wait_for(child.wait(), 5)


async def expire(factory, job_id):
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, job_id, with_for_update=True)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)


async def release_retry(factory, job_id):
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, job_id, with_for_update=True)
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["claimed", "processing"])
async def test_killed_worker_recovered_after_lease_expiry(shared_world, phase):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    job_id, attempt_id = await kill_after_phase(phase)
    assert job_id == str(ids[0])
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        assert row.status == CrawlerJobStatus.RUNNING and row.attempts == 1
        assert row.lease_token is not None
    await expire(factory, ids[0])
    repo = PostgresCrawlerJobRepository(factory)
    assert await asyncio.wait_for(repo.claim_next(tenants[0]), 5) is None
    async with factory() as session:
        first = await session.get(CrawlerJobAttempt, UUID(attempt_id))
        assert first.outcome == "expired" and first.ended_at is not None
    await release_retry(factory, ids[0])
    recovered = await asyncio.wait_for(repo.claim_next(tenants[0], worker_id=uuid4()), 5)
    assert recovered is not None and recovered.attempt_id != attempt_id
    await asyncio.wait_for(repo.persist_result(recovered, result()), 5)
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        attempts = (await session.scalars(select(CrawlerJobAttempt).where(
            CrawlerJobAttempt.job_id == ids[0]).order_by(CrawlerJobAttempt.generation)
        )).all()
        sources = await session.scalar(select(func.count()).select_from(SourceRecord).where(
            SourceRecord.tenant_id == tenants[0]))
    assert row.status == CrawlerJobStatus.COMPLETED and row.attempts == 2
    assert [a.outcome for a in attempts] == ["expired", "completed"]
    assert sources == 1


@pytest.mark.asyncio
async def test_stale_worker_rejected_after_reclaim(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    repo = PostgresCrawlerJobRepository(factory)
    old = await repo.claim_next(tenants[0], worker_id=uuid4())
    assert old is not None
    await expire(factory, ids[0])
    assert await repo.claim_next(tenants[0]) is None
    await release_retry(factory, ids[0])
    current = await repo.claim_next(tenants[0], worker_id=uuid4())
    assert current is not None and current.lease_token != old.lease_token
    assert not await repo.heartbeat(old)
    with pytest.raises(RuntimeError, match="lease"):
        await repo.persist_result(old, result())
    with pytest.raises(RuntimeError, match="lease"):
        await repo.persist_failure(old, "crawler_error")
    await repo.persist_result(current, result())
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        assert row.status == CrawlerJobStatus.COMPLETED and row.attempts == 2
        assert (await session.get(CrawlerJobAttempt, old.attempt_id)).outcome == "expired"


@pytest.mark.asyncio
async def test_competing_recovery_workers_single_current_attempt(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0], 2)
    repo = PostgresCrawlerJobRepository(factory)
    old = [await repo.claim_next(tenants[0]) for _ in ids]
    assert all(old)
    for job_id in ids:
        await expire(factory, job_id)
    await asyncio.wait_for(asyncio.gather(*(repo.claim_next(tenants[0]) for _ in range(4))), 10)
    for job_id in ids:
        await release_retry(factory, job_id)
    gate = asyncio.Event()

    async def contend():
        await gate.wait()
        return await repo.claim_next(tenants[0], worker_id=uuid4())

    tasks = [asyncio.create_task(contend()) for _ in range(4)]
    gate.set()
    claims = [c for c in await asyncio.wait_for(asyncio.gather(*tasks), 10) if c]
    assert len(claims) == 2 and len({c.job_id for c in claims}) == 2
    async with factory() as session:
        attempts = (await session.scalars(select(CrawlerJobAttempt).where(
            CrawlerJobAttempt.job_id.in_(ids)))).all()
    assert len(attempts) == 4
    assert all(len([a for a in attempts if a.job_id == job_id and a.ended_at is None]) == 1
               for job_id in ids)


@pytest.mark.asyncio
async def test_retry_exhaustion_has_five_ledger_rows(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    repo = PostgresCrawlerJobRepository(factory)
    for generation in range(1, 6):
        claim = await asyncio.wait_for(repo.claim_next(tenants[0]), 5)
        assert claim is not None
        await expire(factory, ids[0])
        assert await asyncio.wait_for(repo.claim_next(tenants[0]), 5) is None
        if generation < 5:
            await release_retry(factory, ids[0])
    assert await repo.claim_next(tenants[0]) is None
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        attempts = (await session.scalars(select(CrawlerJobAttempt).where(
            CrawlerJobAttempt.job_id == ids[0]).order_by(CrawlerJobAttempt.generation)
        )).all()
    assert row.status == CrawlerJobStatus.FAILED and row.failure_code == "retry_exhausted"
    assert [a.generation for a in attempts] == [1, 2, 3, 4, 5]
    assert [a.outcome for a in attempts] == ["expired"] * 4 + ["retry_exhausted"]
    assert attempts[-1].failure_code == "retry_exhausted"


@pytest.mark.asyncio
async def test_cancellation_race_rejects_lease_and_result(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    repo = PostgresCrawlerJobRepository(factory)
    claim = await repo.claim_next(tenants[0])
    assert claim is not None
    ctx = await context(factory, tenants[0], users[0])
    gate = asyncio.Event()

    async def cancel():
        async with factory() as session:
            await gate.wait()
            return await cancel_crawler_job(ids[0], request(), ctx, session)

    async def renew():
        await gate.wait()
        return await repo.heartbeat(claim)

    cancel_task = asyncio.create_task(cancel())
    renew_task = asyncio.create_task(renew())
    gate.set()
    cancelled, _renewed = await asyncio.wait_for(asyncio.gather(cancel_task, renew_task), 10)
    assert cancelled.status == CrawlerJobStatus.CANCELLED
    assert not await repo.heartbeat(claim)
    with pytest.raises(RuntimeError, match="lease"):
        await repo.persist_result(claim, result())
    async with factory() as session:
        attempt = await session.get(CrawlerJobAttempt, claim.attempt_id)
        sources = await session.scalar(select(func.count()).select_from(SourceRecord).where(
            SourceRecord.tenant_id == tenants[0]))
    assert attempt.outcome == "cancelled" and attempt.ended_at is not None
    assert sources == 0


@pytest.mark.asyncio
async def test_queued_cancellation_races_claim_without_result(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    ids = await add_jobs(factory, tenants[0], users[0], companies[0])
    repo = PostgresCrawlerJobRepository(factory)
    ctx = await context(factory, tenants[0], users[0])
    gate = asyncio.Event()

    async def claim():
        await gate.wait()
        return await repo.claim_next(tenants[0])

    async def cancel():
        async with factory() as session:
            await gate.wait()
            return await cancel_crawler_job(ids[0], request(), ctx, session)

    tasks = [asyncio.create_task(claim()), asyncio.create_task(cancel())]
    gate.set()
    claimed, cancelled = await asyncio.wait_for(asyncio.gather(*tasks), 10)
    assert cancelled.status == CrawlerJobStatus.CANCELLED
    if claimed is not None:
        assert not await repo.heartbeat(claimed)
        with pytest.raises(RuntimeError, match="lease"):
            await repo.persist_result(claimed, result())
    async with factory() as session:
        row = await session.get(CrawlerJob, ids[0])
        attempts = (await session.scalars(select(CrawlerJobAttempt).where(
            CrawlerJobAttempt.job_id == ids[0]))).all()
        sources = await session.scalar(select(func.count()).select_from(SourceRecord).where(
            SourceRecord.tenant_id == tenants[0]))
    assert row.status == CrawlerJobStatus.CANCELLED
    assert len(attempts) == int(claimed is not None)
    assert all(a.outcome == "cancelled" for a in attempts)
    assert sources == 0
