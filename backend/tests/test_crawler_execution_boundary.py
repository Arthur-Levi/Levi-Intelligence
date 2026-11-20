import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from starlette.requests import Request

from app.api.v1.endpoints import prospecting
from app.crawler_jobs.contracts import (
    CrawlerJobInput,
    CrawlerJobResult,
    build_crawler_job,
    validate_job_input,
)
from app.crawler_jobs.worker import execute_crawler_job, execute_next_crawler_job
from app.models.prospecting import CrawlerJob, CrawlerJobStatus
from app.services.company_enrichment import UnsafeWebsiteError


class MemoryRepository:
    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled
        self.results: list[tuple[CrawlerJobInput, CrawlerJobResult]] = []
        self.failures: list[tuple[CrawlerJobInput, str]] = []
        self.cancellations: list[CrawlerJobInput] = []
        self.claimed_job: CrawlerJobInput | None = None

    async def claim_next(self) -> CrawlerJobInput | None:
        job, self.claimed_job = self.claimed_job, None
        return job

    async def cancellation_requested(self, job_id, tenant_id) -> bool:
        return self.cancelled

    async def persist_result(self, job, result) -> None:
        self.results.append((job, result))

    async def persist_failure(self, job, failure_code) -> None:
        self.failures.append((job, failure_code))

    async def persist_cancellation(self, job) -> None:
        self.cancellations.append(job)


def _job_input() -> CrawlerJobInput:
    return CrawlerJobInput(
        job_id=uuid4(), tenant_id=uuid4(), company_id=uuid4(),
        website_url="https://example.com", idempotency_key="request-0001",
    )


def test_job_creation_preserves_tenant_company_and_is_idempotent() -> None:
    tenant_id, company_id, actor_id = uuid4(), uuid4(), uuid4()
    first = build_crawler_job(
        tenant_id=tenant_id, company_id=company_id, actor_user_id=actor_id,
        website_url="https://example.com", idempotency_key="request-0001",
    )
    retry = build_crawler_job(
        tenant_id=tenant_id, company_id=company_id, actor_user_id=actor_id,
        website_url="https://example.com", idempotency_key="request-0001",
    )
    other_tenant = build_crawler_job(
        tenant_id=uuid4(), company_id=company_id, actor_user_id=actor_id,
        website_url="https://example.com", idempotency_key="request-0001",
    )
    assert first.tenant_id == tenant_id and first.company_id == company_id
    assert first.request_fingerprint == retry.request_fingerprint
    assert first.tenant_id != other_tenant.tenant_id
    assert first.website_url == "https://example.com"


def test_tenant_scoped_job_lookup_requires_both_job_and_tenant() -> None:
    tenant_id, job_id = uuid4(), uuid4()
    statement = select(CrawlerJob).where(CrawlerJob.id == job_id, CrawlerJob.tenant_id == tenant_id)
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "crawler_jobs.id" in sql and "crawler_jobs.tenant_id" in sql


@pytest.mark.asyncio
async def test_api_creates_auditable_tenant_company_job(monkeypatch) -> None:
    tenant_id, company_id, user_id = uuid4(), uuid4(), uuid4()
    company = SimpleNamespace(
        id=company_id, tenant_id=tenant_id, website_url="https://example.com",
        enrichment_status="pending",
    )

    class Session:
        def __init__(self) -> None:
            self.answers = [company, None]
            self.added: list[object] = []
            self.committed = False

        async def scalar(self, statement):
            return self.answers.pop(0)

        def add(self, instance) -> None:
            self.added.append(instance)

        async def flush(self) -> None:
            job = self.added[0]
            job.id = uuid4()
            job.status = CrawlerJobStatus.QUEUED

        async def commit(self) -> None:
            self.committed = True

        async def refresh(self, instance) -> None:
            instance.created_at = datetime.now(UTC)
            instance.started_at = None
            instance.finished_at = None

    async def no_audit(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(prospecting, "record_audit_event", no_audit)
    session = Session()
    request = Request({
        "type": "http", "method": "POST", "path": "/", "headers": [],
        "client": ("127.0.0.1", 1),
    })
    response = await prospecting.create_company_enrichment_job(
        company_id, request, "request-0001",
        SimpleNamespace(tenant=SimpleNamespace(id=tenant_id), user=SimpleNamespace(id=user_id)),
        session,
    )
    assert response.company_id == company_id
    assert response.status == "queued"
    assert session.committed
    assert session.added[0].tenant_id == tenant_id
    assert session.added[0].created_by_user_id == user_id
    assert company.enrichment_status == "queued"


@pytest.mark.asyncio
async def test_cross_tenant_result_lookup_is_hidden_as_not_found() -> None:
    class Session:
        def __init__(self) -> None:
            self.statement = None

        async def scalar(self, statement):
            self.statement = statement
            return None

    tenant_id = uuid4()
    session = Session()
    with pytest.raises(HTTPException) as error:
        await prospecting.get_crawler_job(
            uuid4(), SimpleNamespace(tenant=SimpleNamespace(id=tenant_id)), session
        )
    assert error.value.status_code == 404
    sql = str(session.statement.compile(dialect=postgresql.dialect()))
    assert "crawler_jobs.tenant_id" in sql


def test_payload_is_bounded_and_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CrawlerJobInput(
            job_id=uuid4(), tenant_id=uuid4(), company_id=uuid4(),
            website_url="https://example.com", idempotency_key="request-0001",
            arbitrary_option="unexpected",
        )
    with pytest.raises(ValidationError):
        CrawlerJobInput(
            job_id=uuid4(), tenant_id=uuid4(), company_id=uuid4(),
            website_url="https://" + "a" * 600, idempotency_key="request-0001",
        )


def test_payload_url_syntax_is_checked_before_worker_execution() -> None:
    job = _job_input().model_copy(update={"website_url": "javascript:alert(1)"})
    with pytest.raises(ValueError):
        validate_job_input(job)


@pytest.mark.asyncio
async def test_worker_calls_injected_secure_service_and_persists_result() -> None:
    repository = MemoryRepository()
    job = _job_input()
    calls: list[str] = []

    async def secure_crawler(url: str) -> dict[str, object]:
        calls.append(url)
        return {
            "status": "verified", "status_code": 200,
            "checked_at": datetime.now(UTC), "emails": ["hi@example.com"],
        }

    state = await execute_crawler_job(job, repository=repository, crawler=secure_crawler)
    assert state == "completed"
    assert calls == [job.website_url]
    assert repository.results[0][0].tenant_id == job.tenant_id
    assert repository.results[0][1].emails == ["hi@example.com"]


@pytest.mark.asyncio
async def test_worker_default_invokes_company_enrichment_secure_crawler(monkeypatch) -> None:
    import app.services.company_enrichment as enrichment

    repository = MemoryRepository()
    job = _job_input()
    repository.claimed_job = job
    called: list[str] = []

    async def preserved_secure_service(url: str) -> dict[str, object]:
        called.append(url)
        return {"status": "reachable_non_html", "status_code": 204, "checked_at": datetime.now(UTC)}

    monkeypatch.setattr(enrichment, "verify_and_extract", preserved_secure_service)
    assert await execute_next_crawler_job(repository=repository)
    assert called == [job.website_url]
    assert repository.results[0][0].job_id == job.job_id


@pytest.mark.asyncio
async def test_worker_records_structured_failures_without_exception_text() -> None:
    repository = MemoryRepository()

    async def rejected(url: str) -> dict[str, object]:
        raise UnsafeWebsiteError("do not persist details")

    state = await execute_crawler_job(_job_input(), repository=repository, crawler=rejected)
    assert state == "failed"
    assert repository.failures[0][1] == "unsafe_website"


@pytest.mark.asyncio
async def test_worker_honors_cancellation_before_and_during_execution() -> None:
    before = MemoryRepository(cancelled=True)
    job = _job_input()

    async def should_not_run(url: str) -> dict[str, object]:
        pytest.fail("cancelled job invoked crawler")

    assert await execute_crawler_job(job, repository=before, crawler=should_not_run) == "cancelled"
    assert before.cancellations == [job]

    active = MemoryRepository()
    started = asyncio.Event()

    async def wait_for_cancel(url: str) -> dict[str, object]:
        started.set()
        await asyncio.Event().wait()
        return {}

    task = asyncio.create_task(execute_crawler_job(job, repository=active, crawler=wait_for_cancel))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active.cancellations == [job]
