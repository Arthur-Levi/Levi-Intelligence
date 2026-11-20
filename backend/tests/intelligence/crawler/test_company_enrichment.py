"""Company enrichment integration tests with a synthetic transport response."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.crawler_jobs.contracts import CrawlerJobInput, CrawlerJobResult
from app.crawler_jobs.worker import execute_crawler_job
from app.intelligence.crawler.exceptions import UnsafeTargetError
from app.intelligence.crawler.http_transport import FetchResponse, SecureHTTPTransport
from app.intelligence.crawler.robots_policy import RobotsDeniedError
from app.services.company_enrichment import UnsafeWebsiteError, verify_and_extract


@pytest.mark.asyncio
async def test_company_enrichment_uses_secure_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    async def synthetic_fetch(self: SecureHTTPTransport, url: str) -> FetchResponse:
        assert url == "https://example.com/"
        return FetchResponse(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=b"Contact sales@example.com or +55 11 1234-5678",
        )

    monkeypatch.setattr(SecureHTTPTransport, "fetch", synthetic_fetch)

    async def no_robots_network(self: SecureHTTPTransport, url: str) -> FetchResponse:
        return FetchResponse(url=url, status=404, headers={}, body=b"")

    monkeypatch.setattr(SecureHTTPTransport, "_fetch_robots_document", no_robots_network)

    result = await verify_and_extract("https://example.com")

    assert result["status"] == "verified"
    assert result["emails"] == ["sales@example.com"]
    assert result["phones"] == ["+55 11 1234-5678"]


@pytest.mark.asyncio
async def test_company_enrichment_rejects_private_target_before_transport() -> None:
    with pytest.raises(UnsafeWebsiteError) as exc_info:
        await verify_and_extract("http://127.0.0.1/")

    assert isinstance(exc_info.value.__cause__, UnsafeTargetError)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 503])
async def test_http_error_never_becomes_verified_enrichment(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    async def synthetic_fetch(self: SecureHTTPTransport, url: str) -> FetchResponse:
        return FetchResponse(
            url=url,
            status=status,
            headers={"content-type": "text/html"},
            body=b"contact invented@example.com +1 555 123 4567",
        )

    monkeypatch.setattr(SecureHTTPTransport, "fetch", synthetic_fetch)

    result = await verify_and_extract("https://example.com/")

    assert result["status"] == "http_error"
    assert result["status_code"] == status
    assert "emails" not in result
    assert "phones" not in result


@pytest.mark.asyncio
async def test_robots_denied_page_never_reaches_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def denied_fetch(self: SecureHTTPTransport, url: str) -> FetchResponse:
        raise RobotsDeniedError("synthetic robots denial")

    monkeypatch.setattr(SecureHTTPTransport, "fetch", denied_fetch)

    with pytest.raises(UnsafeWebsiteError) as exc_info:
        await verify_and_extract("https://example.com/private")

    assert isinstance(exc_info.value.__cause__, RobotsDeniedError)


@pytest.mark.asyncio
async def test_malformed_html_and_missing_metadata_produce_no_business_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def synthetic_fetch(self: SecureHTTPTransport, url: str) -> FetchResponse:
        return FetchResponse(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=b"\xff<html><title>Example</title><p>No contact details</p>",
        )

    monkeypatch.setattr(SecureHTTPTransport, "fetch", synthetic_fetch)

    result = await verify_and_extract("https://example.com/")

    assert result["status"] == "verified"
    assert result["emails"] == []
    assert result["phones"] == []


@pytest.mark.asyncio
async def test_http_error_worker_result_keeps_unverified_status() -> None:
    job = CrawlerJobInput(
        job_id=uuid4(), tenant_id=uuid4(), company_id=uuid4(),
        website_url="https://example.com/", idempotency_key="request-0001",
    )

    class Repository:
        result: CrawlerJobResult | None = None

        async def claim_next(self) -> CrawlerJobInput | None:
            return None

        async def cancellation_requested(self, job_id, tenant_id) -> bool:
            return False

        async def persist_result(self, claimed, result: CrawlerJobResult) -> None:
            self.result = result

        async def persist_failure(self, claimed, failure_code: str) -> None:
            pytest.fail(f"unexpected failure: {failure_code}")

        async def persist_cancellation(self, claimed) -> None:
            pytest.fail("unexpected cancellation")

    async def http_error(url: str) -> dict[str, object]:
        return {"status": "http_error", "status_code": 503, "checked_at": datetime.now(UTC)}

    repository = Repository()
    assert await execute_crawler_job(job, repository=repository, crawler=http_error) == "completed"
    assert repository.result is not None
    assert repository.result.status == "http_error"
    assert repository.result.emails == [] and repository.result.phones == []
