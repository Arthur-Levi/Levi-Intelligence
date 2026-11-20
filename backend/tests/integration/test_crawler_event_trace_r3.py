"""End-to-end structured trace through admission, recovery and terminal states."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_postgres_crawler_lifecycle import context, request, result
from test_postgres_crawler_lifecycle import world as shared_world  # noqa: F401

from app.api.v1.endpoints.prospecting import cancel_crawler_job, create_company_enrichment_job
from app.crawler_jobs import events
from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.models.prospecting import CrawlerJob

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_structured_trace_reconstructs_recovery_and_terminals(shared_world, monkeypatch):  # noqa: F811
    factory, tenants, users, companies = shared_world
    captured = []
    monkeypatch.setattr(events.logger, "info", captured.append)
    ctx = await context(factory, tenants[0], users[0])
    repo = PostgresCrawlerJobRepository(factory)

    async def admit(key):
        async with factory() as session:
            return await create_company_enrichment_job(
                companies[0], request(), key, ctx, session)

    completed = await admit("r3-trace-completed")
    first_worker = uuid4()
    first = await repo.claim_next(tenants[0], worker_id=first_worker)
    assert first is not None and first.job_id == completed.id
    assert await repo.heartbeat(first)
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, first.job_id, with_for_update=True)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await repo.claim_next(tenants[0]) is None
    async with factory.begin() as session:
        row = await session.get(CrawlerJob, first.job_id, with_for_update=True)
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    second_worker = uuid4()
    second = await repo.claim_next(tenants[0], worker_id=second_worker)
    assert second is not None and second.attempt_id != first.attempt_id
    await repo.persist_result(second, result())

    cancelled = await admit("r3-trace-cancelled")
    active = await repo.claim_next(tenants[0], worker_id=uuid4())
    assert active is not None and active.job_id == cancelled.id
    async with factory() as session:
        await cancel_crawler_job(cancelled.id, request(), ctx, session)

    failed = await admit("r3-trace-failed")
    failing = await repo.claim_next(tenants[0], worker_id=uuid4())
    assert failing is not None and failing.job_id == failed.id
    await repo.persist_failure(failing, "crawler_error")

    trace = [json.loads(message) for message in captured]
    for item in trace:
        assert set(item) == {"event_type", "timestamp", "tenant_id", "job_id",
                             "attempt_id", "worker_id", "outcome", "correlation_id"}
        assert item["tenant_id"] == str(tenants[0])
        assert "example.test" not in json.dumps(item)
        assert "lease_token" not in item
    by_job = {str(job_id): [item for item in trace if item["job_id"] == str(job_id)]
              for job_id in (completed.id, cancelled.id, failed.id)}
    assert {item["event_type"] for item in by_job[str(completed.id)]} >= {
        "admission", "claim", "lease_renewal", "lease_expiration", "recovery",
        "retry_scheduled", "completion"}
    assert {item["event_type"] for item in by_job[str(cancelled.id)]} >= {
        "admission", "claim", "cancellation"}
    assert {item["event_type"] for item in by_job[str(failed.id)]} >= {
        "admission", "claim", "failure"}
    first_events = [item for item in by_job[str(completed.id)]
                    if item["attempt_id"] == str(first.attempt_id)]
    assert {item["event_type"] for item in first_events} >= {
        "claim", "lease_renewal", "lease_expiration", "recovery", "retry_scheduled"}
    assert all(item["worker_id"] == str(first_worker) for item in first_events)
    second_events = [item for item in by_job[str(completed.id)]
                     if item["attempt_id"] == str(second.attempt_id)]
    assert {item["event_type"] for item in second_events} == {"claim", "completion"}
    assert all(item["worker_id"] == str(second_worker) for item in second_events)
