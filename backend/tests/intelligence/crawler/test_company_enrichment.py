"""Company enrichment integration tests with a synthetic transport response."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.v1.endpoints import prospecting
from app.intelligence.crawler.exceptions import UnsafeTargetError
from app.intelligence.crawler.http_transport import FetchResponse, SecureHTTPTransport
from app.intelligence.crawler.robots_policy import RobotsDeniedError
from app.models.prospecting import Company, CompanyStatus
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
async def test_http_error_does_not_mark_company_enrichment_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    company = Company(
        id=uuid4(),
        tenant_id=uuid4(),
        name="Example Company",
        website_url="https://example.com/",
        address={},
        status=CompanyStatus.NEW,
        qualification_reasons=[],
        created_at=now,
        updated_at=now,
        website_status="unknown",
        enrichment_status="pending",
        qualification_breakdown={},
    )

    class Session:
        async def scalar(self, statement: object) -> Company:
            return company

        def add(self, instance: object) -> None:
            return None

        async def commit(self) -> None:
            return None

        async def refresh(self, instance: object) -> None:
            return None

    async def http_error(url: str) -> dict[str, object]:
        return {"status": "http_error", "status_code": 503, "checked_at": now}

    async def no_audit(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(prospecting, "verify_and_extract", http_error)
    monkeypatch.setattr(prospecting, "record_audit_event", no_audit)

    await prospecting.enrich_company(
        company.id,
        SimpleNamespace(client=None),  # type: ignore[arg-type]
        SimpleNamespace(
            tenant=SimpleNamespace(id=company.tenant_id),
            user=SimpleNamespace(id=uuid4()),
        ),  # type: ignore[arg-type]
        Session(),  # type: ignore[arg-type]
    )

    assert company.website_status == "http_error"
    assert company.enrichment_status == "failed"
    assert company.last_verified_at is None
