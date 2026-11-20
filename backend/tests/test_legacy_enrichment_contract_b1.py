"""Legacy HTTP URL now delegates to the canonical durable admission contract."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request

from app.api.v1.endpoints import prospecting
from app.main import app
from app.models.prospecting import Company, CompanyStatus
from app.schemas.prospecting import CrawlerJobResponse


def _company(website_url: str | None = "https://example.test") -> Company:
    now = datetime.now(UTC)
    return Company(
        id=uuid4(), tenant_id=uuid4(), name="Synthetic company",
        website_url=website_url, address={}, status=CompanyStatus.NEW,
        qualification_reasons=[], created_at=now, updated_at=now,
        website_status="unknown", enrichment_status="pending",
        qualification_breakdown={},
    )


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                    "client": ("127.0.0.1", 1)})


def _context(company: Company) -> SimpleNamespace:
    return SimpleNamespace(tenant=SimpleNamespace(id=company.tenant_id),
                           user=SimpleNamespace(id=uuid4()))


def test_both_openapi_contracts_require_key_and_return_accepted_job() -> None:
    paths = app.openapi()["paths"]
    old = paths["/api/v1/companies/{company_id}/enrich"]["post"]
    canonical = paths["/api/v1/companies/{company_id}/enrich/jobs"]["post"]
    for operation in (old, canonical):
        assert operation["responses"]["202"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/CrawlerJobResponse")
        assert any(parameter["name"] == "Idempotency-Key" and parameter["required"]
                   for parameter in operation["parameters"])
    assert old["security"] == canonical["security"] == [{"HTTPBearer": []}]


@pytest.mark.asyncio
async def test_legacy_route_delegates_to_canonical_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company = _company()
    calls: list[tuple[object, ...]] = []

    async def canonical(*args: object) -> CrawlerJobResponse:
        calls.append(args)
        return CrawlerJobResponse(
            id=uuid4(), company_id=company.id, status="queued", created_at=datetime.now(UTC)
        )

    monkeypatch.setattr(prospecting, "create_company_enrichment_job", canonical)
    session = object()
    response = await prospecting.enrich_company(
        company.id, _request(), "request-0001", _context(company), session  # type: ignore[arg-type]
    )
    assert response.status == "queued"
    assert len(calls) == 1
    assert calls[0][0] == company.id and calls[0][2] == "request-0001"
    assert calls[0][4] is session


@pytest.mark.asyncio
@pytest.mark.parametrize("found,website,status_code", [
    (False, "https://example.test", 404), (True, None, 422),
])
async def test_legacy_route_missing_company_or_website(
    found: bool, website: str | None, status_code: int,
) -> None:
    company = _company(website)

    class Session:
        async def scalar(self, statement: object) -> Company | None:
            return company if found else None

    with pytest.raises(HTTPException) as error:
        await prospecting.enrich_company(
            company.id, _request(), "request-0001", _context(company), Session(),  # type: ignore[arg-type]
        )
    assert error.value.status_code == status_code
