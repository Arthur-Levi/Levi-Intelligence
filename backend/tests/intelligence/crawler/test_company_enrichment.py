"""Company enrichment integration tests with a synthetic transport response."""

from __future__ import annotations

import pytest

from app.intelligence.crawler.exceptions import UnsafeTargetError
from app.intelligence.crawler.http_transport import FetchResponse, SecureHTTPTransport
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

    result = await verify_and_extract("https://example.com")

    assert result["status"] == "verified"
    assert result["emails"] == ["sales@example.com"]
    assert result["phones"] == ["+55 11 1234-5678"]


@pytest.mark.asyncio
async def test_company_enrichment_rejects_private_target_before_transport() -> None:
    with pytest.raises(UnsafeWebsiteError) as exc_info:
        await verify_and_extract("http://127.0.0.1/")

    assert isinstance(exc_info.value.__cause__, UnsafeTargetError)
