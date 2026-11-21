"""Capacity and ownership checks for expired crawler leases."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from crawler_faults import fixture_id
from fastapi import HTTPException
from sqlalchemy import delete, func, insert, select
from test_crawler_admission_r3 import admit
from test_postgres_crawler_lifecycle import add_jobs, context, request, result
from test_postgres_crawler_lifecycle import world as shared_world  # noqa: F401

from app.api.v1.endpoints.prospecting import cancel_crawler_job, get_crawler_job
from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.models.identity import Tenant
from app.models.prospecting import Company, CrawlerJob, CrawlerJobAttempt, CrawlerJobStatus

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_expired_lease_does_not_overflow_tenant_queue(shared_world):  # noqa: F811
    """Expected to fail against B.3: recovery makes the 101st queued row."""
    factory, tenants, users, companies = shared_world
    repo = PostgresCrawlerJobRepository(factory)
    expired_id = (await add_jobs(factory, tenants[0], users[0], companies[0]))[0]
    claim = await repo.claim_next(tenants[0])
    assert claim is not None and claim.job_id == expired_id
    await add_jobs(factory, tenants[0], users[0], companies[0], 100)
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, expired_id, with_for_update=True)
        assert row is not None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await repo.claim_next(tenants[1])
    async with factory() as session:
        queued = await session.scalar(select(func.count()).select_from(CrawlerJob).where(
            CrawlerJob.tenant_id == tenants[0], CrawlerJob.status == CrawlerJobStatus.QUEUED,
        ))
    assert queued == 100


@pytest.mark.asyncio
async def test_pending_ledger_progress_fencing_and_priority(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    repo = PostgresCrawlerJobRepository(factory)
    expired_id = (await add_jobs(factory, tenants[0], users[0], companies[0]))[0]
    old_claim = await repo.claim_next(tenants[0])
    assert old_claim is not None
    filler = await add_jobs(factory, tenants[0], users[0], companies[0], 100)
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, expired_id, with_for_update=True)
        assert row is not None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    await asyncio.gather(*(repo.claim_next(tenants[1]) for _ in range(3)))
    async with factory() as session:
        pending = await session.get(CrawlerJob, expired_id)
        ledger = await session.scalar(select(CrawlerJobAttempt).where(
            CrawlerJobAttempt.job_id == expired_id,
        ))
        assert pending is not None and pending.status == CrawlerJobStatus.RUNNING
        assert pending.lease_token is None and pending.lease_expires_at <= datetime.now(UTC)
        assert pending.attempts == 1
        assert ledger is not None and ledger.outcome == "expired" and ledger.ended_at is not None
        assert await session.scalar(select(func.count()).select_from(CrawlerJob).where(
            CrawlerJob.tenant_id == tenants[0],
            CrawlerJob.status == CrawlerJobStatus.RUNNING,
            CrawlerJob.lease_expires_at > datetime.now(UTC),
        )) == 0
        first_end = ledger.ended_at
    ctx = await context(factory, tenants[0], users[0])
    async with factory() as session:
        public = await get_crawler_job(expired_id, ctx, session)
    assert public.status == CrawlerJobStatus.RUNNING.value

    # Once the tenant has room, admission yields the slot to recovery.
    async with factory.begin() as session:
        await session.execute(delete(CrawlerJob).where(CrawlerJob.id == filler[0]))
    with pytest.raises(HTTPException) as rejected:
        await admit(factory, tenants[0], users[0], companies[0], "b4-priority-new-admission")
    assert rejected.value.status_code == 429
    await asyncio.sleep(2.1)  # First-attempt exponential retry delay.
    await repo.claim_next(tenants[1])
    async with factory() as session:
        queued = await session.get(CrawlerJob, expired_id)
        ledger = await session.scalar(select(CrawlerJobAttempt).where(
            CrawlerJobAttempt.job_id == expired_id,
        ))
        assert queued is not None and queued.status == CrawlerJobStatus.QUEUED
        assert ledger is not None and ledger.ended_at == first_end
    async with factory.begin() as session:
        await session.execute(delete(CrawlerJob).where(CrawlerJob.id.in_(filler[1:])))
        queued = await session.get(CrawlerJob, expired_id, with_for_update=True)
        assert queued is not None
        queued.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    new_claim = await repo.claim_next(tenants[0])
    assert new_claim is not None and new_claim.job_id == expired_id
    assert new_claim.lease_token != old_claim.lease_token
    with pytest.raises(RuntimeError, match="lease"):
        await repo.persist_result(old_claim, result())
    with pytest.raises(RuntimeError, match="lease"):
        await repo.persist_failure(old_claim, "crawler_error")
    async with factory() as session:
        rows = (await session.scalars(select(CrawlerJobAttempt).where(
            CrawlerJobAttempt.job_id == expired_id,
        ).order_by(CrawlerJobAttempt.generation))).all()
        assert [entry.generation for entry in rows] == [1, 2]
        assert [entry.outcome for entry in rows] == ["expired", "running"]


@pytest.mark.asyncio
async def test_cancellation_and_exhaustion_need_no_queue_slot(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    repo = PostgresCrawlerJobRepository(factory)
    ids = await add_jobs(factory, tenants[0], users[0], companies[0], 2)
    claims = [await repo.claim_next(tenants[0]) for _ in ids]
    assert all(claim is not None for claim in claims)
    await add_jobs(factory, tenants[0], users[0], companies[0], 100)
    async with factory.begin() as session:
        for index, job_id in enumerate(ids):
            row = await session.get(CrawlerJob, job_id, with_for_update=True)
            assert row is not None
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            if index == 1:
                ledger = await session.scalar(select(CrawlerJobAttempt).where(
                    CrawlerJobAttempt.job_id == job_id,
                ))
                assert ledger is not None
                ledger.generation = 5
                row.attempts = 5
    # Cancellation can terminate pending recovery while the tenant is full.
    await repo.claim_next(tenants[1])
    ctx = await context(factory, tenants[0], users[0])
    async with factory() as session:
        cancelled = await cancel_crawler_job(ids[0], request(), ctx, session)
    assert cancelled.status == CrawlerJobStatus.CANCELLED
    async with factory() as session:
        exhausted = await session.get(CrawlerJob, ids[1])
        assert exhausted is not None and exhausted.status == CrawlerJobStatus.FAILED
        assert exhausted.failure_code == "retry_exhausted"
        assert await session.scalar(select(func.count()).select_from(CrawlerJob).where(
            CrawlerJob.tenant_id == tenants[0],
            CrawlerJob.status == CrawlerJobStatus.QUEUED,
        )) == 100


@pytest.mark.asyncio
async def test_exact_global_and_tenant_saturation(shared_world):  # noqa: F811
    """Real PostgreSQL counts at 10,000 global and 100 for the pending tenant."""
    factory, tenants, users, companies = shared_world
    repo = PostgresCrawlerJobRepository(factory)
    expired_id = (await add_jobs(factory, tenants[0], users[0], companies[0]))[0]
    assert await repo.claim_next(tenants[0]) is not None
    local_filler = await add_jobs(factory, tenants[0], users[0], companies[0], 100)
    extra_tenants = [fixture_id("b4-global", f"tenant-{i}") for i in range(99)]
    extra_companies = [fixture_id("b4-global", f"company-{i}") for i in range(99)]
    try:
        async with factory.begin() as session:
            await session.execute(insert(Tenant), [
                {"id": tenant, "name": "Synthetic B4 tenant", "slug": f"b4-{tenant.hex}"}
                for tenant in extra_tenants
            ])
            await session.execute(insert(Company), [
                {"id": extra_companies[i], "tenant_id": tenant,
                 "name": "Synthetic B4 company", "normalized_domain": f"b4-{i}.example.test",
                 "website_url": "https://example.test"}
                for i, tenant in enumerate(extra_tenants)
            ])
        for start in range(0, 9900, 500):
            rows = []
            for n in range(start, min(start + 500, 9900)):
                index, number = divmod(n, 100)
                rows.append({
                    "id": fixture_id("b4-global", f"job-{n}"),
                    "tenant_id": extra_tenants[index],
                    "company_id": extra_companies[index],
                    "created_by_user_id": users[0],
                    "website_url": "https://example.test",
                    "idempotency_key": f"b4-global-{number}",
                    "request_fingerprint": "a" * 64,
                    "status": CrawlerJobStatus.QUEUED,
                })
            async with factory.begin() as session:
                await session.execute(insert(CrawlerJob), rows)
        async with factory.begin() as session:
            row = await session.get(CrawlerJob, expired_id, with_for_update=True)
            assert row is not None
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await repo.claim_next(tenants[1])
        async with factory() as session:
            started = time.perf_counter()
            global_count = await session.scalar(select(func.count()).select_from(CrawlerJob).where(
                CrawlerJob.status == CrawlerJobStatus.QUEUED,
            ))
            print(f"B4 global queued count at 10000: {time.perf_counter() - started:.3f}s")
            assert global_count == 10_000
            assert (await session.get(CrawlerJob, expired_id)).status == CrawlerJobStatus.RUNNING
        # Global room alone does not admit a retry into a full tenant.
        async with factory.begin() as session:
            await session.execute(delete(CrawlerJob).where(
                CrawlerJob.id == fixture_id("b4-global", "job-0"),
            ))
        await repo.claim_next(tenants[1])
        async with factory() as session:
            assert (await session.get(CrawlerJob, expired_id)).status == CrawlerJobStatus.RUNNING
        # A pending retry in a full tenant does not block an unrelated tenant.
        started = time.perf_counter()
        unrelated = await admit(factory, tenants[1], users[1], companies[1],
                                "b4-global-unrelated")
        print(f"B4 unrelated admission at 9999 queued: {time.perf_counter() - started:.3f}s")
        assert unrelated.status == CrawlerJobStatus.QUEUED
        # Both rooms are needed; then the retry becomes queued without overflow.
        async with factory.begin() as session:
            await session.execute(delete(CrawlerJob).where(CrawlerJob.id == local_filler[0]))
        started = time.perf_counter()
        with pytest.raises(HTTPException) as reserved:
            await admit(factory, tenants[1], users[1], companies[1],
                        "b4-global-priority")
        print(f"B4 correlated reservation at 9999 queued: {time.perf_counter() - started:.3f}s")
        assert reserved.value.status_code == 429
        await asyncio.sleep(2.1)
        started = time.perf_counter()
        await repo.claim_next(uuid4())
        print(f"B4 recovery at 9999 queued: {time.perf_counter() - started:.3f}s")
        async with factory() as session:
            assert (await session.get(CrawlerJob, expired_id)).status == CrawlerJobStatus.QUEUED
            assert await session.scalar(select(func.count()).select_from(CrawlerJob).where(
                CrawlerJob.status == CrawlerJobStatus.QUEUED,
            )) == 10_000
            assert await session.scalar(select(func.count()).select_from(CrawlerJob).where(
                CrawlerJob.tenant_id == tenants[0],
                CrawlerJob.status == CrawlerJobStatus.QUEUED,
            )) == 100
    finally:
        async with factory.begin() as session:
            await session.execute(delete(CrawlerJob).where(CrawlerJob.tenant_id.in_(extra_tenants)))
            await session.execute(delete(Company).where(Company.tenant_id.in_(extra_tenants)))
            await session.execute(delete(Tenant).where(Tenant.id.in_(extra_tenants)))


@pytest.mark.asyncio
async def test_admission_races_multiple_recovery_workers_for_last_tenant_slot(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    repo = PostgresCrawlerJobRepository(factory)
    job_id = (await add_jobs(factory, tenants[0], users[0], companies[0]))[0]
    assert await repo.claim_next(tenants[0]) is not None
    await add_jobs(factory, tenants[0], users[0], companies[0], 99)
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, job_id, with_for_update=True)
        assert row is not None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    gate = asyncio.Event()

    async def recover():
        await gate.wait()
        return await repo.claim_next(tenants[1])

    async def submit(index: int):
        await gate.wait()
        try:
            return await admit(factory, tenants[0], users[0], companies[0],
                               f"b4-admission-recovery-race-{index}")
        except HTTPException as exc:
            return exc

    tasks = [asyncio.create_task(recover()) for _ in range(4)]
    tasks.extend(asyncio.create_task(submit(index)) for index in range(12))
    gate.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), 15)
    assert all(isinstance(item, HTTPException) and item.status_code == 429
               for item in results[4:])
    async with factory() as session:
        row = await session.get(CrawlerJob, job_id)
        ledger = await session.scalar(select(CrawlerJobAttempt).where(
            CrawlerJobAttempt.job_id == job_id,
        ))
        assert row is not None and row.status == CrawlerJobStatus.QUEUED
        assert row.attempts == 1 and row.lease_token is None
        assert ledger is not None and ledger.outcome == "expired"
        assert await session.scalar(select(func.count()).select_from(CrawlerJob).where(
            CrawlerJob.tenant_id == tenants[0], CrawlerJob.status == CrawlerJobStatus.QUEUED,
        )) == 100


@pytest.mark.asyncio
async def test_saturated_tenant_does_not_block_other_tenant_recovery(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    repo = PostgresCrawlerJobRepository(factory)
    ids = []
    for index in range(2):
        ids.append((await add_jobs(factory, tenants[index], users[index], companies[index]))[0])
        assert await repo.claim_next(tenants[index]) is not None
    await add_jobs(factory, tenants[0], users[0], companies[0], 100)
    await add_jobs(factory, tenants[1], users[1], companies[1], 99)
    async with factory.begin() as session:
        for job_id in ids:
            row = await session.get(CrawlerJob, job_id, with_for_update=True)
            assert row is not None
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await repo.claim_next(uuid4())
    async with factory() as session:
        rows = [await session.get(CrawlerJob, job_id) for job_id in ids]
        assert rows[0] is not None and rows[0].status == CrawlerJobStatus.RUNNING
        assert rows[1] is not None and rows[1].status == CrawlerJobStatus.QUEUED
        assert await session.scalar(select(func.count()).select_from(CrawlerJob).where(
            CrawlerJob.tenant_id == tenants[1], CrawlerJob.status == CrawlerJobStatus.QUEUED,
        )) == 100


@pytest.mark.asyncio
async def test_bounded_scan_rotates_past_saturated_tenant(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    repo = PostgresCrawlerJobRepository(factory)
    blocked = await add_jobs(factory, tenants[0], users[0], companies[0], 33)
    eligible = (await add_jobs(factory, tenants[1], users[1], companies[1]))[0]
    async with factory.begin() as session:
        for index, job_id in enumerate([*blocked, eligible]):
            row = await session.get(CrawlerJob, job_id, with_for_update=True)
            assert row is not None
            row.status = CrawlerJobStatus.RUNNING
            row.attempts = 1
            row.started_at = datetime.now(UTC) - timedelta(minutes=2)
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=100 - index)
            session.add(CrawlerJobAttempt(
                tenant_id=row.tenant_id, job_id=row.id, generation=1,
                worker_id=users[index == 33], claimed_at=row.started_at,
                ended_at=row.lease_expires_at, outcome="expired",
            ))
    await add_jobs(factory, tenants[0], users[0], companies[0], 100)
    await add_jobs(factory, tenants[1], users[1], companies[1], 99)
    await repo.claim_next(uuid4())
    async with factory() as session:
        assert (await session.get(CrawlerJob, eligible)).status == CrawlerJobStatus.RUNNING
    await repo.claim_next(uuid4())
    async with factory() as session:
        assert (await session.get(CrawlerJob, eligible)).status == CrawlerJobStatus.QUEUED
        assert await session.scalar(select(func.count()).select_from(CrawlerJob).where(
            CrawlerJob.tenant_id == tenants[1], CrawlerJob.status == CrawlerJobStatus.QUEUED,
        )) == 100


@pytest.mark.asyncio
async def test_claim_skips_more_than_64_older_jobs_in_active_saturated_tenant(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    repo = PostgresCrawlerJobRepository(factory)
    await add_jobs(factory, tenants[0], users[0], companies[0], 2)
    assert await repo.claim_next(tenants[0]) is not None
    assert await repo.claim_next(tenants[0]) is not None
    await add_jobs(factory, tenants[0], users[0], companies[0], 65)
    eligible = (await add_jobs(factory, tenants[1], users[1], companies[1]))[0]
    claim = await repo.claim_next()
    assert claim is not None and claim.job_id == eligible


@pytest.mark.asyncio
async def test_admission_bounds_recovery_backlog_per_tenant(shared_world):  # noqa: F811
    factory, tenants, users, companies = shared_world
    pending_ids = await add_jobs(factory, tenants[0], users[0], companies[0], 3)
    async with factory.begin() as session:
        for job_id in pending_ids:
            row = await session.get(CrawlerJob, job_id, with_for_update=True)
            assert row is not None
            row.status = CrawlerJobStatus.RUNNING
            row.attempts = 1
            row.started_at = datetime.now(UTC) - timedelta(minutes=2)
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            row.next_attempt_at = datetime.now(UTC) + timedelta(hours=1)
    filler = await add_jobs(factory, tenants[0], users[0], companies[0], 99)
    with pytest.raises(HTTPException) as rejected:
        await admit(factory, tenants[0], users[0], companies[0], "b4-backlog-over-cap")
    assert rejected.value.status_code == 429
    assert rejected.value.detail == {"code": "crawler_tenant_queue_full"}
    async with factory.begin() as session:
        await session.execute(delete(CrawlerJob).where(CrawlerJob.id == filler[0]))
    accepted = await admit(factory, tenants[0], users[0], companies[0], "b4-backlog-final")
    assert accepted.status == CrawlerJobStatus.QUEUED
    async with factory() as session:
        queued = await session.scalar(select(func.count()).select_from(CrawlerJob).where(
            CrawlerJob.tenant_id == tenants[0], CrawlerJob.status == CrawlerJobStatus.QUEUED,
        ))
        running = await session.scalar(select(func.count()).select_from(CrawlerJob).where(
            CrawlerJob.tenant_id == tenants[0], CrawlerJob.status == CrawlerJobStatus.RUNNING,
        ))
        assert queued == 99 and running == 3 and queued + running == 102
