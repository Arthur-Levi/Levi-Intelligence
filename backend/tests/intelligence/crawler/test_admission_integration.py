from __future__ import annotations

import asyncio

import pytest

from app.intelligence.crawler import http_transport
from app.intelligence.crawler.admission import AdmissionConfig, AdmissionController
from app.intelligence.crawler.http_transport import FetchResponse, SecureHTTPTransport
from app.intelligence.crawler.url_validator import ValidatedURL
from app.services.company_enrichment import verify_and_extract


class AllowRobots:
    async def check(self, url: str, fetcher: object) -> None:
        return None


@pytest.fixture(autouse=True)
def isolate_optional_robots_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these admission tests independent of pending robots changes."""
    monkeypatch.setattr(
        http_transport,
        "get_default_robots_policy",
        lambda: AllowRobots(),
        raising=False,
    )


class AdmissionProbeTransport(SecureHTTPTransport):
    def __init__(self, admission: AdmissionController, responses: list[FetchResponse]) -> None:
        super().__init__(admission_controller=admission)
        self.admission = admission
        self.responses = iter(responses)
        self.entered: list[str] = []

    async def _request_once(self, target: ValidatedURL) -> FetchResponse:
        assert self.admission.active_requests > 0
        self.entered.append(target.url)
        return next(self.responses)


@pytest.mark.asyncio
async def test_each_redirect_hop_uses_admission_and_releases_between_hops() -> None:
    admission = AdmissionController(AdmissionConfig(burst=10))
    transport = AdmissionProbeTransport(
        admission,
        [
            FetchResponse(
                "https://example.com/", 302, {"location": "https://other.example.com/"}, b""
            ),
            FetchResponse("https://other.example.com/", 200, {}, b"ok"),
        ],
    )

    result = await transport.fetch("https://example.com/")

    assert result.body == b"ok"
    assert transport.entered == ["https://example.com/", "https://other.example.com/"]
    assert admission.active_requests == 0
    assert admission.tracked_hosts == 2


@pytest.mark.asyncio
async def test_transport_concurrency_respects_global_host_and_cross_host_limits() -> None:
    admission = AdmissionController(
        AdmissionConfig(global_limit=2, per_host_limit=1, max_waiters=4, burst=20)
    )
    entered = asyncio.Event()
    finish = asyncio.Event()
    currently_active = 0
    peak_active = 0
    per_host: dict[str, int] = {}

    class BlockingTransport(SecureHTTPTransport):
        def __init__(self) -> None:
            super().__init__(admission_controller=admission)

        async def _request_once(self, target: ValidatedURL) -> FetchResponse:
            nonlocal currently_active, peak_active
            assert admission.active_requests <= 2
            currently_active += 1
            peak_active = max(peak_active, currently_active)
            per_host[target.hostname] = per_host.get(target.hostname, 0) + 1
            assert per_host[target.hostname] <= 1
            entered.set()
            try:
                await finish.wait()
                return FetchResponse(target.url, 200, {}, b"ok")
            finally:
                per_host[target.hostname] -= 1
                currently_active -= 1

    transport = BlockingTransport()
    tasks = [
        asyncio.create_task(transport.fetch("https://one.example.com/1")),
        asyncio.create_task(transport.fetch("https://one.example.com/2")),
        asyncio.create_task(transport.fetch("https://two.example.com/")),
    ]
    await asyncio.wait_for(entered.wait(), 1)
    for _ in range(20):
        if admission.queued_requests == 1:
            break
        await asyncio.sleep(0)
    assert admission.active_requests == 2
    assert admission.queued_requests == 1
    assert peak_active == 2

    finish.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 1)
    assert admission.active_requests == 0
    assert admission.queued_requests == 0


@pytest.mark.asyncio
async def test_request_exception_and_cancellation_release_admission() -> None:
    admission = AdmissionController(AdmissionConfig(burst=10))

    class FailingTransport(SecureHTTPTransport):
        async def _request_once(self, target: ValidatedURL) -> FetchResponse:
            assert admission.active_requests == 1
            raise ValueError("synthetic transport failure")

    failing = FailingTransport(admission_controller=admission)
    with pytest.raises(ValueError, match="synthetic transport failure"):
        await failing.fetch("https://example.com/")
    assert admission.active_requests == 0

    started = asyncio.Event()
    never_finish = asyncio.Event()

    class WaitingTransport(SecureHTTPTransport):
        async def _request_once(self, target: ValidatedURL) -> FetchResponse:
            assert admission.active_requests == 1
            started.set()
            await never_finish.wait()
            return FetchResponse(target.url, 200, {}, b"")

    waiting = WaitingTransport(admission_controller=admission)
    task = asyncio.create_task(waiting.fetch("https://example.com/"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert admission.active_requests == 0
    assert admission.queued_requests == 0


@pytest.mark.asyncio
async def test_company_enrichment_uses_shared_controlled_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = AdmissionController(AdmissionConfig(burst=10))
    monkeypatch.setattr(http_transport, "get_default_admission_controller", lambda: admission)
    called: list[str] = []

    async def synthetic_request(self: SecureHTTPTransport, target: ValidatedURL) -> FetchResponse:
        called.append(target.url)
        assert admission.active_requests == 1
        return FetchResponse(
            target.url,
            200,
            {"content-type": "text/html"},
            b"Contact team@example.com",
        )

    monkeypatch.setattr(SecureHTTPTransport, "_request_once", synthetic_request)

    result = await verify_and_extract("https://example.com/")

    assert result["status"] == "verified"
    assert result["emails"] == ["team@example.com"]
    assert called == ["https://example.com/"]
    assert admission.active_requests == 0
