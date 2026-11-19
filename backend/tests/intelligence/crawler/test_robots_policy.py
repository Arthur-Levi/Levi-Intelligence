from __future__ import annotations

import asyncio

import pytest

from app.intelligence.crawler.admission import AdmissionConfig, AdmissionController
from app.intelligence.crawler.exceptions import (
    DNSResolutionError,
    RobotsDeniedError,
    TooManyRedirectsError,
    UnsafeTargetError,
)
from app.intelligence.crawler.http_transport import FetchResponse, SecureHTTPTransport
from app.intelligence.crawler.robots_policy import RobotsPolicy, RobotsPolicyError, RobotsRules


def response(status: int, body: bytes = b"") -> FetchResponse:
    return FetchResponse("https://example.com/robots.txt", status, {}, body)


def test_rules_cover_user_agent_allow_disallow_specificity_and_wildcards() -> None:
    rules = RobotsRules.parse(
        b"""User-agent: LeviIntelligenceBot
Disallow: /secret
Allow: /secret/open
""",
        user_agent="LeviIntelligenceBot",
    )
    assert not rules.allows("https://example.com/secret")
    assert rules.allows("https://example.com/secret/open")
    wildcard = RobotsRules.parse(
        b"User-agent: *\nDisallow: /private*\nAllow: /private/public$\n",
        user_agent="LeviIntelligenceBot",
    )
    assert wildcard.allows("https://example.com/private/public")
    assert not wildcard.allows("https://example.com/private/hidden")


def test_general_group_and_path_query_normalization() -> None:
    rules = RobotsRules.parse(
        b"User-agent: *\nDisallow: /search?private=1\n",
        user_agent="LeviIntelligenceBot",
    )
    assert not rules.allows("https://example.com/search?private=1")
    assert rules.allows("https://example.com/search?public=1")


def test_percent_encoded_unreserved_path_characters_are_normalized() -> None:
    rules = RobotsRules.parse(
        b"User-agent: *\nDisallow: /~private\nDisallow: /caf%C3%A9\n",
        user_agent="LeviIntelligenceBot",
    )
    assert not rules.allows("https://example.com/%7Eprivate")
    assert not rules.allows("https://example.com/caf%c3%a9")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"user_agent": "OtherCrawler"},
        {"ttl_seconds": 0},
        {"ttl_seconds": float("inf")},
        {"max_entries": 0},
        {"max_inflight_origins": True},
        {"max_waiters_per_origin": -1},
        {"max_bytes": 1.5},
    ],
)
def test_rejects_invalid_robots_policy_config(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RobotsPolicy(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "allowed"),
    [
        (200, b"User-agent: *\nDisallow: /blocked\n", False),
        (404, b"", True),
        (401, b"", False),
        (403, b"", False),
    ],
)
async def test_policy_status_handling(status: int, body: bytes, allowed: bool) -> None:
    policy = RobotsPolicy()
    calls: list[str] = []

    async def fetcher(url: str) -> FetchResponse:
        calls.append(url)
        return response(status, body)

    url = "https://example.com/blocked"
    if allowed:
        await policy.check(url, fetcher)
    else:
        with pytest.raises(RobotsDeniedError):
            await policy.check(url, fetcher)
    assert calls == ["https://example.com/robots.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [TimeoutError("synthetic"), DNSResolutionError("synthetic")],
)
async def test_transient_failure_fails_closed_and_is_not_cached(exc: Exception) -> None:
    policy = RobotsPolicy()
    calls = 0

    async def fail(url: str) -> FetchResponse:
        nonlocal calls
        calls += 1
        raise exc

    for _ in range(2):
        with pytest.raises(RobotsPolicyError):
            await policy.check("https://example.com/", fail)
    assert calls == 2
    assert policy.cached_origins == 0


@pytest.mark.asyncio
async def test_5xx_and_malformed_or_oversized_content_fail_closed() -> None:
    policy = RobotsPolicy(max_bytes=100)

    async def server_error(url: str) -> FetchResponse:
        return response(503)

    with pytest.raises(RobotsPolicyError):
        await policy.check("https://example.com/", server_error)

    async def malformed(url: str) -> FetchResponse:
        return response(200, b"not a directive")

    with pytest.raises(RobotsDeniedError):
        await policy.check("https://malformed.example.com/blocked", malformed)

    small_policy = RobotsPolicy(max_bytes=8)
    async def oversized(url: str) -> FetchResponse:
        return response(200, b"User-agent: *\n")

    with pytest.raises(RobotsPolicyError, match="byte limit"):
        await small_policy.check("https://large.example.com/", oversized)


@pytest.mark.asyncio
async def test_cache_hit_expiration_eviction_and_bounded_size() -> None:
    now = [0.0]
    policy = RobotsPolicy(ttl_seconds=10, max_entries=1, clock=lambda: now[0])
    calls: list[str] = []

    async def missing(url: str) -> FetchResponse:
        calls.append(url)
        return response(404)

    await policy.check("https://one.example.com/a", missing)
    await policy.check("https://one.example.com/b", missing)
    assert len(calls) == 1
    await policy.check("https://two.example.com/", missing)
    assert policy.cached_origins == 1
    await policy.check("https://one.example.com/", missing)
    assert len(calls) == 3
    now[0] = 11
    await policy.check("https://one.example.com/", missing)
    assert len(calls) == 4
    assert policy.cached_origins == 1


@pytest.mark.asyncio
async def test_simultaneous_same_origin_requests_are_coalesced() -> None:
    policy = RobotsPolicy()
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def fetcher(url: str) -> FetchResponse:
        nonlocal calls
        calls += 1
        entered.set()
        await finish.wait()
        return response(404)

    tasks = [
        asyncio.create_task(policy.check("https://same.example.com/page", fetcher))
        for _ in range(12)
    ]
    await entered.wait()
    await asyncio.sleep(0)
    finish.set()
    await asyncio.gather(*tasks)
    assert calls == 1
    assert policy.cached_origins == 1


@pytest.mark.asyncio
async def test_transport_integrates_robots_admission_and_redirect_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = AdmissionController(AdmissionConfig(burst=50))
    policy = RobotsPolicy()
    transport = SecureHTTPTransport(admission_controller=admission, robots_policy=policy)
    seen: list[str] = []

    async def synthetic(target: object) -> FetchResponse:
        url = target.url  # type: ignore[attr-defined]
        seen.append(url)
        if url.endswith("/robots.txt"):
            return response(404)
        if url.endswith("/start"):
            return FetchResponse(url, 302, {"location": "https://127.0.0.1/private"}, b"")
        return FetchResponse(url, 200, {}, b"ok")

    monkeypatch.setattr(transport, "_request_once", synthetic)
    with pytest.raises(UnsafeTargetError, match="IP-literal"):
        await transport.fetch("https://example.com/start")
    assert seen == ["https://example.com/robots.txt", "https://example.com/start"]
    assert admission.active_requests == 0


@pytest.mark.asyncio
async def test_transport_performs_robots_before_page_and_admits_each_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = AdmissionController(AdmissionConfig(burst=20))
    policy = RobotsPolicy()
    transport = SecureHTTPTransport(admission_controller=admission, robots_policy=policy)
    seen: list[str] = []

    async def synthetic(target: object) -> FetchResponse:
        url = target.url  # type: ignore[attr-defined]
        seen.append(url)
        if url.endswith("/robots.txt"):
            return response(200, b"User-agent: *\nDisallow: /nope")
        return FetchResponse(url, 200, {}, b"ok")

    monkeypatch.setattr(transport, "_request_once", synthetic)
    with pytest.raises(RobotsDeniedError):
        await transport.fetch("https://example.com/nope")
    assert seen == ["https://example.com/robots.txt"]
    assert admission.active_requests == 0


@pytest.mark.asyncio
async def test_safe_redirect_gets_a_separate_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    class RecordingAdmission(AdmissionController):
        def __init__(self) -> None:
            super().__init__(AdmissionConfig(burst=20))
            self.acquisitions: list[str] = []

        async def acquire(self, hostname: str) -> None:
            self.acquisitions.append(self.normalize_hostname(hostname))
            await super().acquire(hostname)

    admission = RecordingAdmission()
    transport = SecureHTTPTransport(admission_controller=admission, robots_policy=RobotsPolicy())
    seen: list[str] = []

    async def synthetic(target: object) -> FetchResponse:
        url = target.url  # type: ignore[attr-defined]
        seen.append(url)
        if url.endswith("/robots.txt"):
            return response(404)
        if url.endswith("/start"):
            return FetchResponse(url, 302, {"location": "/next"}, b"")
        return FetchResponse(url, 200, {}, b"ok")

    monkeypatch.setattr(transport, "_request_once", synthetic)
    result = await transport.fetch("https://example.com/start")
    assert result.url == "https://example.com/next"
    assert seen == [
        "https://example.com/robots.txt",
        "https://example.com/start",
        "https://example.com/next",
    ]
    assert admission.acquisitions == ["example.com"] * 3
    assert admission.active_requests == 0


@pytest.mark.asyncio
async def test_robots_redirect_across_origins_applies_rules_to_requesting_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingAdmission(AdmissionController):
        def __init__(self) -> None:
            super().__init__(AdmissionConfig(burst=20))
            self.acquisitions: list[str] = []

        async def acquire(self, hostname: str) -> None:
            self.acquisitions.append(self.normalize_hostname(hostname))
            await super().acquire(hostname)

    admission = RecordingAdmission()
    policy = RobotsPolicy()
    transport = SecureHTTPTransport(admission_controller=admission, robots_policy=policy)
    seen: list[str] = []

    async def synthetic(target: object) -> FetchResponse:
        url = target.url  # type: ignore[attr-defined]
        seen.append(url)
        if url == "https://origin.example.com/robots.txt":
            return FetchResponse(url, 302, {"location": "https://policy.example.com/rules"}, b"")
        if url == "https://policy.example.com/rules":
            return FetchResponse(url, 200, {}, b"User-agent: *\nDisallow: /blocked\n")
        return FetchResponse(url, 200, {}, b"page")

    monkeypatch.setattr(transport, "_request_once", synthetic)
    result = await transport.fetch("https://origin.example.com/allowed")

    assert result.url == "https://origin.example.com/allowed"
    assert seen == [
        "https://origin.example.com/robots.txt",
        "https://policy.example.com/rules",
        "https://origin.example.com/allowed",
    ]
    assert admission.acquisitions == [
        "origin.example.com",
        "policy.example.com",
        "origin.example.com",
    ]
    assert policy.cached_origins == 1
    assert admission.active_requests == 0

    with pytest.raises(RobotsDeniedError):
        await transport.fetch("https://origin.example.com/blocked")
    assert seen == [
        "https://origin.example.com/robots.txt",
        "https://policy.example.com/rules",
        "https://origin.example.com/allowed",
    ]


@pytest.mark.asyncio
async def test_robots_redirect_to_unsafe_destination_is_blocked_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = SecureHTTPTransport(robots_policy=RobotsPolicy())
    seen: list[str] = []

    async def synthetic(target: object) -> FetchResponse:
        url = target.url  # type: ignore[attr-defined]
        seen.append(url)
        return FetchResponse(url, 302, {"location": "http://127.0.0.1/robots.txt"}, b"")

    monkeypatch.setattr(transport, "_request_once", synthetic)
    with pytest.raises(RobotsPolicyError) as exc_info:
        await transport.fetch("https://origin.example.com/page")
    assert isinstance(exc_info.value.__cause__, UnsafeTargetError)
    assert seen == ["https://origin.example.com/robots.txt"]


@pytest.mark.asyncio
async def test_robots_redirect_limit_is_enforced_without_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = SecureHTTPTransport(max_redirects=1, robots_policy=RobotsPolicy())
    seen: list[str] = []

    async def synthetic(target: object) -> FetchResponse:
        url = target.url  # type: ignore[attr-defined]
        seen.append(url)
        if url == "https://origin.example.com/robots.txt":
            return FetchResponse(url, 302, {"location": "https://one.example.com/r"}, b"")
        return FetchResponse(url, 302, {"location": "https://two.example.com/r"}, b"")

    monkeypatch.setattr(transport, "_request_once", synthetic)
    with pytest.raises(RobotsPolicyError) as exc_info:
        await transport.fetch("https://origin.example.com/page")
    assert isinstance(exc_info.value.__cause__, TooManyRedirectsError)
    assert seen == ["https://origin.example.com/robots.txt", "https://one.example.com/r"]


@pytest.mark.asyncio
async def test_robots_redirect_destination_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = SecureHTTPTransport(robots_policy=RobotsPolicy())
    seen: list[str] = []

    async def synthetic(target: object) -> FetchResponse:
        url = target.url  # type: ignore[attr-defined]
        seen.append(url)
        if url == "https://origin.example.com/robots.txt":
            return FetchResponse(url, 302, {"location": "https://policy.example.com/rules"}, b"")
        raise TimeoutError("synthetic destination timeout")

    monkeypatch.setattr(transport, "_request_once", synthetic)
    with pytest.raises(RobotsPolicyError, match="could not be retrieved safely"):
        await transport.fetch("https://origin.example.com/page")
    assert seen == ["https://origin.example.com/robots.txt", "https://policy.example.com/rules"]


@pytest.mark.asyncio
async def test_cancelled_transport_request_releases_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = AdmissionController(AdmissionConfig(burst=20))
    transport = SecureHTTPTransport(admission_controller=admission, robots_policy=RobotsPolicy())
    entered = asyncio.Event()
    never = asyncio.Event()

    async def synthetic(target: object) -> FetchResponse:
        if target.url.endswith("/robots.txt"):  # type: ignore[attr-defined]
            return response(404)
        entered.set()
        await never.wait()
        return FetchResponse(target.url, 200, {}, b"")  # type: ignore[attr-defined]

    monkeypatch.setattr(transport, "_request_once", synthetic)
    task = asyncio.create_task(transport.fetch("https://cancel.example.com/page"))
    await entered.wait()
    assert admission.active_requests == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert admission.active_requests == 0
