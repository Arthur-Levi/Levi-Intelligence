"""Transport policy tests use synthetic responses and never contact websites."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from urllib.parse import urlsplit

import aiohttp
import pytest

from app.intelligence.crawler import http_transport
from app.intelligence.crawler.dns_guard import PinnedResolver
from app.intelligence.crawler.exceptions import (
    FetchError,
    FetchTimeoutError,
    ResponseTooLargeError,
    TooManyRedirectsError,
    UnsafeTargetError,
)
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


class FakeDNSGuard:
    """Test-only guard returns a documentation address; no socket is opened."""

    def __init__(self) -> None:
        self.hosts: list[str] = []
        self.closed = False

    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        self.hosts.append(hostname)
        return ("192.0.2.1",)

    async def close(self) -> None:
        self.closed = True


class FakeConnector:
    def __init__(self, *, resolver: PinnedResolver, **kwargs: object) -> None:
        self.resolver = resolver
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeContent:
    def __init__(self, chunks: list[bytes], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error


class FakeResponseContext:
    def __init__(self, session: FakeSession, url: str) -> None:
        self.session = session
        self.url = url

    async def __aenter__(self) -> FakeResponse:
        if self.session.connection_error is not None:
            raise self.session.connection_error
        parsed = urlsplit(self.url)
        await self.session.connector.resolver.resolve(
            parsed.hostname or "example.com", parsed.port or 80
        )
        return self.session.response

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
        stream_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(chunks or [], stream_error)


class FakeSession:
    def __init__(
        self,
        *,
        connector: FakeConnector,
        response: FakeResponse,
        connection_error: Exception | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> None:
        self.connector = connector
        self.response = response
        self.connection_error = connection_error
        self.timeout = timeout
        self.closed = False

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True
        await self.connector.close()

    def get(self, url: str, **kwargs: object) -> FakeResponseContext:
        return FakeResponseContext(self, url)


def install_http_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response_factory: Callable[[], FakeResponse],
    connection_error: Exception | None = None,
    guard_factory: Callable[[], FakeDNSGuard] = FakeDNSGuard,
) -> tuple[list[FakeSession], list[FakeDNSGuard]]:
    sessions: list[FakeSession] = []
    guards: list[FakeDNSGuard] = []

    def make_guard() -> FakeDNSGuard:
        guard = guard_factory()
        guards.append(guard)
        return guard

    def make_connector(**kwargs: object) -> FakeConnector:
        return FakeConnector(**kwargs)  # type: ignore[arg-type]

    def make_session(*, connector: FakeConnector, **kwargs: object) -> FakeSession:
        session = FakeSession(
            connector=connector,
            response=response_factory(),
            connection_error=connection_error,
            timeout=kwargs.get("timeout"),  # type: ignore[arg-type]
        )
        sessions.append(session)
        return session

    monkeypatch.setattr(http_transport, "DNSGuard", make_guard)
    monkeypatch.setattr(http_transport.aiohttp, "TCPConnector", make_connector)
    monkeypatch.setattr(http_transport.aiohttp, "ClientSession", make_session)
    return sessions, guards


def allow_test_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    def validate(url: str) -> ValidatedURL:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            raise UnsafeTargetError("Only HTTP and HTTPS URLs are allowed")
        hostname = parsed.hostname or "example.com"
        path = parsed.path or "/"
        normalized = f"{parsed.scheme}://{hostname}{path}"
        if parsed.query:
            normalized += f"?{parsed.query}"
        port = 443 if parsed.scheme == "https" else 80
        return ValidatedURL(normalized, parsed.scheme, hostname, port, path, parsed.query)

    monkeypatch.setattr(http_transport, "validate_url", validate)


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


@pytest.mark.asyncio
async def test_streaming_response_at_byte_limit_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_http_mocks(
        monkeypatch,
        response_factory=lambda: FakeResponse(chunks=[b"1234", b"5678"]),
    )
    allow_test_urls(monkeypatch)
    result = await SecureHTTPTransport(max_response_bytes=8).fetch("http://example.com/")
    assert result.body == b"12345678"


@pytest.mark.asyncio
async def test_streaming_response_over_byte_limit_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_http_mocks(
        monkeypatch,
        response_factory=lambda: FakeResponse(chunks=[b"1234", b"5678"]),
    )
    allow_test_urls(monkeypatch)
    with pytest.raises(ResponseTooLargeError):
        await SecureHTTPTransport(max_response_bytes=7).fetch("http://example.com/")


@pytest.mark.asyncio
async def test_no_content_length_response_is_read_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_http_mocks(
        monkeypatch,
        response_factory=lambda: FakeResponse(headers={"x-test": "ok"}, chunks=[b"small body"]),
    )
    allow_test_urls(monkeypatch)
    result = await SecureHTTPTransport().fetch("http://example.com/")
    assert result.body == b"small body"
    assert result.headers == {"x-test": "ok"}


@pytest.mark.asyncio
async def test_incorrect_content_length_is_reported_as_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_http_mocks(
        monkeypatch,
        response_factory=lambda: FakeResponse(
            headers={"content-length": "20"},
            stream_error=aiohttp.ClientPayloadError("response payload is shorter than declared"),
        ),
    )
    allow_test_urls(monkeypatch)
    with pytest.raises(FetchError):
        await SecureHTTPTransport().fetch("http://example.com/")


@pytest.mark.asyncio
async def test_read_timeout_is_translated_and_owned_dns_resolver_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions, guards = install_http_mocks(
        monkeypatch,
        response_factory=lambda: FakeResponse(
            headers={"content-length": "4"},
            stream_error=TimeoutError(),
        ),
    )
    allow_test_urls(monkeypatch)
    with pytest.raises(FetchTimeoutError):
        await SecureHTTPTransport(timeout_seconds=0.02).fetch("http://example.com/")
    assert guards[0].closed
    assert sessions[0].closed
    assert sessions[0].timeout is not None
    assert sessions[0].timeout.total == 0.02


@pytest.mark.asyncio
async def test_connection_timeout_is_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions, _ = install_http_mocks(
        monkeypatch,
        response_factory=FakeResponse,
        connection_error=TimeoutError(),
    )
    allow_test_urls(monkeypatch)
    with pytest.raises(FetchTimeoutError):
        await SecureHTTPTransport(timeout_seconds=0.02).fetch("http://example.com/")
    assert sessions[0].timeout is not None
    assert sessions[0].timeout.sock_connect == 0.02


@pytest.mark.asyncio
async def test_connection_failure_is_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    install_http_mocks(
        monkeypatch,
        response_factory=FakeResponse,
        connection_error=aiohttp.ClientConnectionError("simulated connection failure"),
    )
    allow_test_urls(monkeypatch)
    with pytest.raises(FetchError):
        await SecureHTTPTransport(timeout_seconds=0.2).fetch("http://example.com/")


@pytest.mark.asyncio
async def test_redirect_to_another_hostname_re_resolves_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            FakeResponse(status=302, headers={"location": "http://other.example/second"}),
            FakeResponse(chunks=[b"ok"]),
        ]
    )
    sessions, guards = install_http_mocks(
        monkeypatch, response_factory=lambda: next(responses)
    )
    allow_test_urls(monkeypatch)
    result = await SecureHTTPTransport().fetch("http://example.com/first")
    assert result.body == b"ok"
    assert guards[0].hosts == ["example.com"]
    assert guards[1].hosts == ["other.example"]
    assert len(sessions) == 2


@pytest.mark.asyncio
async def test_redirect_with_forbidden_scheme_is_rejected_before_second_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_http_mocks(
        monkeypatch,
        response_factory=lambda: FakeResponse(
            status=302, headers={"location": "file:///etc/passwd"}
        ),
    )
    allow_test_urls(monkeypatch)
    with pytest.raises(UnsafeTargetError):
        await SecureHTTPTransport().fetch("http://example.com/")


@pytest.mark.asyncio
async def test_redirect_chain_over_limit_stops_at_configured_hops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_http_mocks(
        monkeypatch,
        response_factory=lambda: FakeResponse(status=302, headers={"location": "/next"}),
    )
    allow_test_urls(monkeypatch)
    with pytest.raises(TooManyRedirectsError):
        await SecureHTTPTransport(max_redirects=2).fetch("http://example.com/")


@pytest.mark.asyncio
async def test_error_closes_session_connector_and_owned_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions, guards = install_http_mocks(
        monkeypatch,
        response_factory=lambda: FakeResponse(chunks=[b"12345678"]),
    )
    allow_test_urls(monkeypatch)
    with pytest.raises(ResponseTooLargeError):
        await SecureHTTPTransport(max_response_bytes=4).fetch("http://example.com/")
    assert guards[0].closed
    assert sessions[0].closed
    assert sessions[0].connector is None or sessions[0].connector.closed
