"""Transport policy tests use synthetic responses and never contact websites."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp.abc import AbstractResolver, ResolveResult

from app.intelligence.crawler import http_transport
from app.intelligence.crawler.dns_guard import DNSGuard, PinnedResolver
from app.intelligence.crawler.exceptions import (
    DNSResolutionError,
    FetchError,
    FetchTimeoutError,
    ResponseTooLargeError,
    TooManyRedirectsError,
    UnsafeTargetError,
)
from app.intelligence.crawler.http_transport import FetchResponse, SecureHTTPTransport
from app.intelligence.crawler.robots_policy import RobotsPolicy
from app.intelligence.crawler.url_validator import ValidatedURL


@pytest.fixture(autouse=True)
def bypass_robots_in_transport_boundary_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests isolate HTTP mechanics; policy integration has dedicated tests."""

    async def allow(self: RobotsPolicy, url: str, fetcher: object) -> None:
        return None

    monkeypatch.setattr(RobotsPolicy, "check", allow)


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


class ControlledResolver(AbstractResolver):
    def __init__(self, answers: list[list[ResolveResult] | OSError]) -> None:
        self.answers = iter(answers)
        self.hosts: list[str] = []
        self.closed = False

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[ResolveResult]:
        self.hosts.append(host)
        answer = next(self.answers)
        if isinstance(answer, OSError):
            raise answer
        return answer

    async def close(self) -> None:
        self.closed = True


def dns_record(address: str, hostname: str = "example.com") -> ResolveResult:
    return ResolveResult(
        hostname=hostname,
        host=address,
        port=80,
        family=socket.AF_INET6 if ":" in address else socket.AF_INET,
        proto=socket.IPPROTO_TCP,
        flags=0,
    )


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


@pytest.mark.parametrize("timeout_seconds", [0, -0.1, float("nan"), float("inf")])
def test_rejects_invalid_timeout_configuration(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        SecureHTTPTransport(timeout_seconds=timeout_seconds)


@pytest.mark.parametrize("max_response_bytes", [0, -1])
def test_rejects_invalid_response_byte_limit(max_response_bytes: int) -> None:
    with pytest.raises(ValueError, match="max_response_bytes"):
        SecureHTTPTransport(max_response_bytes=max_response_bytes)


def test_rejects_negative_redirect_limit() -> None:
    with pytest.raises(ValueError, match="max_redirects"):
        SecureHTTPTransport(max_redirects=-1)


@pytest.mark.asyncio
async def test_default_timeout_sets_total_connect_and_read_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions, _ = install_http_mocks(
        monkeypatch, response_factory=lambda: FakeResponse(chunks=[b"ok"])
    )
    allow_test_urls(monkeypatch)

    await SecureHTTPTransport().fetch("http://example.com/")

    timeout = sessions[0].timeout
    assert timeout is not None
    assert timeout.total == 8.0
    assert timeout.sock_connect == 4.0
    assert timeout.sock_read == 8.0


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
    assert sessions[0].timeout.sock_read == 0.02


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
async def test_injected_dns_guard_is_reused_and_remains_caller_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            FakeResponse(status=302, headers={"location": "http://other.example/next"}),
            FakeResponse(chunks=[b"ok"]),
        ]
    )
    install_http_mocks(monkeypatch, response_factory=lambda: next(responses))
    allow_test_urls(monkeypatch)
    guard = FakeDNSGuard()

    result = await SecureHTTPTransport(dns_guard=guard).fetch("http://example.com/")

    assert result.body == b"ok"
    assert guard.hosts == ["example.com", "other.example"]
    assert not guard.closed


@pytest.mark.asyncio
async def test_same_hostname_is_resolved_again_after_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            FakeResponse(status=302, headers={"location": "/next"}),
            FakeResponse(chunks=[b"ok"]),
        ]
    )
    install_http_mocks(monkeypatch, response_factory=lambda: next(responses))
    allow_test_urls(monkeypatch)
    guard = FakeDNSGuard()

    await SecureHTTPTransport(dns_guard=guard).fetch("http://example.com/")

    assert guard.hosts == ["example.com", "example.com"]
    assert not guard.closed


@pytest.mark.asyncio
async def test_dns_failure_on_redirect_stops_before_following_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ControlledResolver(
        [[dns_record("8.8.8.8")], socket.gaierror("simulated DNS failure")]
    )
    responses = iter(
        [
            FakeResponse(status=302, headers={"location": "http://other.example/next"}),
            FakeResponse(chunks=[b"must not be read"]),
        ]
    )
    install_http_mocks(monkeypatch, response_factory=lambda: next(responses))
    allow_test_urls(monkeypatch)

    with pytest.raises(DNSResolutionError):
        await SecureHTTPTransport(dns_guard=DNSGuard(resolver)).fetch(
            "http://example.com/"
        )

    assert resolver.hosts == ["example.com", "other.example"]
    assert not resolver.closed


@pytest.mark.asyncio
async def test_non_public_dns_answer_on_redirect_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ControlledResolver(
        [[dns_record("8.8.8.8")], [dns_record("192.168.1.20", "other.example")]]
    )
    responses = iter(
        [
            FakeResponse(status=302, headers={"location": "http://other.example/next"}),
            FakeResponse(chunks=[b"must not be read"]),
        ]
    )
    install_http_mocks(monkeypatch, response_factory=lambda: next(responses))
    allow_test_urls(monkeypatch)

    with pytest.raises(UnsafeTargetError):
        await SecureHTTPTransport(dns_guard=DNSGuard(resolver)).fetch(
            "http://example.com/"
        )

    assert resolver.hosts == ["example.com", "other.example"]
    assert not resolver.closed


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


@pytest.mark.asyncio
async def test_connector_construction_failure_closes_owned_dns_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guards: list[FakeDNSGuard] = []

    def make_guard() -> FakeDNSGuard:
        guard = FakeDNSGuard()
        guards.append(guard)
        return guard

    def fail_connector(**kwargs: object) -> FakeConnector:
        raise RuntimeError("synthetic connector construction failure")

    monkeypatch.setattr(http_transport, "DNSGuard", make_guard)
    monkeypatch.setattr(http_transport.aiohttp, "TCPConnector", fail_connector)
    allow_test_urls(monkeypatch)

    with pytest.raises(RuntimeError, match="connector construction"):
        await SecureHTTPTransport().fetch("http://example.com/")

    assert len(guards) == 1
    assert guards[0].closed


@pytest.mark.asyncio
async def test_session_construction_failure_closes_connector_and_owned_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connectors: list[FakeConnector] = []
    guards: list[FakeDNSGuard] = []

    def make_guard() -> FakeDNSGuard:
        guard = FakeDNSGuard()
        guards.append(guard)
        return guard

    def make_connector(**kwargs: object) -> FakeConnector:
        connector = FakeConnector(**kwargs)  # type: ignore[arg-type]
        connectors.append(connector)
        return connector

    def fail_session(**kwargs: object) -> FakeSession:
        raise RuntimeError("synthetic session construction failure")

    monkeypatch.setattr(http_transport, "DNSGuard", make_guard)
    monkeypatch.setattr(http_transport.aiohttp, "TCPConnector", make_connector)
    monkeypatch.setattr(http_transport.aiohttp, "ClientSession", fail_session)
    allow_test_urls(monkeypatch)

    with pytest.raises(RuntimeError, match="session construction"):
        await SecureHTTPTransport().fetch("http://example.com/")

    assert connectors[0].closed
    assert guards[0].closed


@pytest.mark.asyncio
async def test_same_host_redirect_rejects_dns_rebinding_before_second_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ControlledResolver(
        [[dns_record("8.8.8.8")], [dns_record("10.0.0.8")]]
    )
    responses = iter(
        [
            FakeResponse(status=302, headers={"location": "/next"}),
            FakeResponse(chunks=[b"must not be read"]),
        ]
    )
    sessions, _ = install_http_mocks(monkeypatch, response_factory=lambda: next(responses))
    allow_test_urls(monkeypatch)
    guard = DNSGuard(resolver)

    with pytest.raises(UnsafeTargetError):
        await SecureHTTPTransport(dns_guard=guard).fetch("http://example.com/")

    assert resolver.hosts == ["example.com", "example.com"]
    assert len(sessions) == 2


@pytest.mark.asyncio
async def test_cancellation_closes_session_connector_and_owned_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions, guards = install_http_mocks(
        monkeypatch, response_factory=lambda: FakeResponse(chunks=[])
    )
    allow_test_urls(monkeypatch)
    stream_entered = asyncio.Event()
    never_release = asyncio.Event()

    async def blocked_stream(content: FakeContent, size: int) -> AsyncIterator[bytes]:
        stream_entered.set()
        await never_release.wait()
        yield b"unreachable"

    monkeypatch.setattr(FakeContent, "iter_chunked", blocked_stream)
    task = asyncio.create_task(SecureHTTPTransport().fetch("http://example.com/"))
    await asyncio.wait_for(stream_entered.wait(), timeout=1.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert sessions[0].closed
    assert sessions[0].connector.closed
    assert guards[0].closed
