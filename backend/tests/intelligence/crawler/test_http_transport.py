"""Transport policy tests use synthetic responses and never contact websites."""

from __future__ import annotations

import pytest

from app.intelligence.crawler.exceptions import TooManyRedirectsError, UnsafeTargetError
from app.intelligence.crawler.http_transport import FetchResponse, SecureHTTPTransport
from app.intelligence.crawler.url_validator import ValidatedURL


class SyntheticTransport(SecureHTTPTransport):
    def __init__(self, responses: list[FetchResponse], **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.responses = iter(responses)
        self.requested: list[ValidatedURL] = []

    async def _request_once(self, target: ValidatedURL) -> FetchResponse:
        self.requested.append(target)
        return next(self.responses)


@pytest.mark.asyncio
async def test_redirect_is_followed_manually_after_url_validation() -> None:
    transport = SyntheticTransport(
        [
            FetchResponse("https://example.com/start", 302, {"location": "/next"}, b""),
            FetchResponse("https://example.com/next", 200, {"content-type": "text/plain"}, b"ok"),
        ]
    )

    result = await transport.fetch("https://example.com/start")

    assert result.status == 200
    assert [request.url for request in transport.requested] == [
        "https://example.com/start",
        "https://example.com/next",
    ]


@pytest.mark.asyncio
async def test_redirect_to_non_public_target_is_rejected_before_request() -> None:
    transport = SyntheticTransport(
        [FetchResponse("https://example.com/", 302, {"location": "http://127.0.0.1/"}, b"")]
    )

    with pytest.raises(UnsafeTargetError):
        await transport.fetch("https://example.com/")
    assert len(transport.requested) == 1


@pytest.mark.asyncio
async def test_redirect_limit_is_enforced() -> None:
    transport = SyntheticTransport(
        [
            FetchResponse("https://example.com/1", 302, {"location": "/2"}, b""),
            FetchResponse("https://example.com/2", 302, {"location": "/3"}, b""),
        ],
        max_redirects=1,
    )

    with pytest.raises(TooManyRedirectsError):
        await transport.fetch("https://example.com/1")
    assert len(transport.requested) == 2
