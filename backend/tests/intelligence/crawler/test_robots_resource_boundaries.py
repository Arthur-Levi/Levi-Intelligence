from __future__ import annotations

import asyncio
import gc
import weakref

import pytest

from app.intelligence.crawler.admission import AdmissionConfig, AdmissionController
from app.intelligence.crawler.http_transport import FetchResponse
from app.intelligence.crawler.robots_policy import (
    RobotsPolicy,
    RobotsPolicyError,
    get_default_robots_policy,
)


def _response(status: int = 404) -> FetchResponse:
    return FetchResponse("https://synthetic.example.com/robots.txt", status, {}, b"")


async def _wait_for(predicate: object, *, turns: int = 1_000) -> None:
    """Yield deterministically until a synchronization predicate becomes true."""
    check = predicate
    assert callable(check)
    for _ in range(turns):
        if check():  # type: ignore[operator]
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached within the bounded event-loop turns")


@pytest.mark.asyncio
async def test_cache_volume_lru_ttl_and_no_stale_entries() -> None:
    now = [0.0]
    policy = RobotsPolicy(max_entries=7, ttl_seconds=10, clock=lambda: now[0])
    calls: dict[str, int] = {}

    async def fetcher(url: str) -> FetchResponse:
        calls[url] = calls.get(url, 0) + 1
        return _response()

    for index in range(80):
        await policy.check(f"https://host-{index}.synthetic.com/page", fetcher)
        assert policy.cached_origins <= 7
        assert len(policy._cache) <= 7

    # A hit promotes the oldest retained entry before inserting another origin.
    lru_origin = "https://host-73.synthetic.com"
    await policy.check(f"{lru_origin}/hit", fetcher)
    await policy.check("https://host-80.synthetic.com/", fetcher)
    assert lru_origin in policy._cache
    assert "https://host-74.synthetic.com" not in policy._cache
    assert len(policy._cache) == 7

    now[0] = 11.0
    await policy.check("https://host-80.synthetic.com/again", fetcher)
    assert len(policy._cache) == 1
    assert set(policy._cache) == {"https://host-80.synthetic.com"}
    assert policy.inflight_origins == 0
    assert calls["https://host-80.synthetic.com/robots.txt"] == 2


@pytest.mark.asyncio
async def test_concurrent_cache_fills_evict_without_exceeding_capacity() -> None:
    policy = RobotsPolicy(max_entries=3, max_inflight_origins=4)
    entered: set[str] = set()
    all_entered = asyncio.Event()
    release = asyncio.Event()

    async def fetcher(url: str) -> FetchResponse:
        entered.add(url)
        if len(entered) == 3:
            all_entered.set()
        await release.wait()
        return _response()

    async def missing(url: str) -> FetchResponse:
        return _response()

    await policy.check("https://old.synthetic.com/", missing)
    tasks = [
        asyncio.create_task(
            policy.check(f"https://fill-{index}.synthetic.com/", fetcher)
        )
        for index in range(3)
    ]
    await asyncio.wait_for(all_entered.wait(), timeout=2)
    assert policy.inflight_origins == 3
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    assert policy.cached_origins == 3
    assert len(policy._cache) == 3
    assert "https://old.synthetic.com" not in policy._cache
    assert policy.inflight_origins == 0
    assert policy.pending_checks == 0


@pytest.mark.asyncio
async def test_failures_are_not_cached_and_later_attempt_recovers() -> None:
    policy = RobotsPolicy(max_entries=2)
    attempts = 0

    async def intermittent(url: str) -> FetchResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic DNS failure")
        return _response()

    with pytest.raises(RobotsPolicyError):
        await policy.check("https://retry.synthetic.com/", intermittent)
    assert policy.cached_origins == 0
    assert policy.inflight_origins == 0
    await policy.check("https://retry.synthetic.com/", intermittent)
    assert attempts == 2
    assert policy.cached_origins == 1


@pytest.mark.asyncio
async def test_singleflight_saturation_and_cancelled_leader_recover() -> None:
    policy = RobotsPolicy(max_inflight_origins=1, max_waiters_per_origin=2)
    first_started = asyncio.Event()
    cancel_first = asyncio.Event()
    attempts = 0

    async def fetcher(url: str) -> FetchResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_started.set()
            await cancel_first.wait()
        return _response()

    leader = asyncio.create_task(policy.check("https://shared.synthetic.com/", fetcher))
    await first_started.wait()
    origin = "https://shared.synthetic.com"
    waiters = [
        asyncio.create_task(policy.check("https://shared.synthetic.com/page", fetcher))
        for _ in range(2)
    ]
    await _wait_for(lambda: policy._inflight[origin].waiters == 2)

    with pytest.raises(RobotsPolicyError, match="capacity is full"):
        await policy.check("https://shared.synthetic.com/overflow", fetcher)
    with pytest.raises(RobotsPolicyError, match="capacity is full"):
        await policy.check("https://other.synthetic.com/", fetcher)
    assert policy.inflight_origins == 1
    assert policy._inflight[origin].waiters == 2

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    await asyncio.wait_for(asyncio.gather(*waiters), timeout=2)
    assert attempts == 2
    assert policy.inflight_origins == 0
    assert policy.cached_origins == 1
    assert origin not in policy._inflight


@pytest.mark.asyncio
async def test_policy_lock_contention_has_a_finite_pending_check_limit() -> None:
    policy = RobotsPolicy(max_inflight_origins=1, max_waiters_per_origin=2)
    await policy._lock.acquire()
    calls = 0

    async def fetcher(url: str) -> FetchResponse:
        nonlocal calls
        calls += 1
        return _response()

    tasks = [
        asyncio.create_task(policy.check("https://lock.synthetic.com/", fetcher))
        for _ in range(4)
    ]
    await _wait_for(lambda: policy.pending_checks == policy.max_pending_checks)
    with pytest.raises(RobotsPolicyError, match="check capacity is full"):
        await tasks[3]
    assert policy.pending_checks == 3
    policy._lock.release()
    await asyncio.wait_for(asyncio.gather(*tasks[:3]), timeout=2)
    assert calls == 1
    assert policy.pending_checks == 0
    assert policy.inflight_origins == 0


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_cancel_shared_load_or_leave_count() -> None:
    policy = RobotsPolicy(max_waiters_per_origin=4)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fetcher(url: str) -> FetchResponse:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _response()

    leader = asyncio.create_task(policy.check("https://wait.synthetic.com/", fetcher))
    await started.wait()
    origin = "https://wait.synthetic.com"
    waiters = [
        asyncio.create_task(policy.check("https://wait.synthetic.com/page", fetcher))
        for _ in range(5)
    ]
    await _wait_for(lambda: policy._inflight[origin].waiters == 4)
    with pytest.raises(RobotsPolicyError, match="too many requests"):
        await policy.check("https://wait.synthetic.com/rejected", fetcher)

    waiters[0].cancel()
    waiters[1].cancel()
    for task in waiters[:2]:
        with pytest.raises(asyncio.CancelledError):
            await task
    await _wait_for(lambda: policy._inflight[origin].waiters == 2)
    release.set()
    await asyncio.wait_for(asyncio.gather(leader, *waiters[2:4]), timeout=2)
    with pytest.raises(RobotsPolicyError, match="too many requests"):
        await waiters[4]
    assert calls == 1
    assert policy.inflight_origins == 0
    assert policy._inflight == {}
    assert policy.cached_origins == 1


@pytest.mark.asyncio
async def test_distinct_origins_load_concurrently_and_leave_no_flights() -> None:
    policy = RobotsPolicy(max_inflight_origins=3, max_waiters_per_origin=1)
    entered: set[str] = set()
    all_entered = asyncio.Event()
    release = asyncio.Event()

    async def fetcher(url: str) -> FetchResponse:
        entered.add(url)
        if len(entered) == 3:
            all_entered.set()
        await release.wait()
        return _response()

    tasks = [
        asyncio.create_task(policy.check(f"https://origin-{i}.synthetic.com/", fetcher))
        for i in range(3)
    ]
    await asyncio.wait_for(all_entered.wait(), timeout=2)
    assert policy.inflight_origins == 3
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    assert policy.inflight_origins == 0
    assert policy.cached_origins == 3


@pytest.mark.asyncio
async def test_integrated_saturation_timeout_eviction_and_recovery() -> None:
    policy = RobotsPolicy(max_entries=3, max_inflight_origins=2, max_waiters_per_origin=1)
    admission = AdmissionController(
        AdmissionConfig(global_limit=1, per_host_limit=1, max_waiters=1, max_hosts=64, burst=100)
    )
    fetch_started: set[str] = set()
    both_started = asyncio.Event()
    release = asyncio.Event()
    attempts: dict[str, int] = {}

    async def synthetic_fetcher(url: str) -> FetchResponse:
        hostname = url.split("/")[2]
        attempts[hostname] = attempts.get(hostname, 0) + 1
        fetch_started.add(hostname)
        if len(fetch_started) >= 2:
            both_started.set()
        async with admission.request(hostname):
            await release.wait()
            if hostname == "load-0.synthetic.com" and attempts[hostname] == 1:
                raise TimeoutError("synthetic timeout under saturation")
            return _response()

    tasks = [
        asyncio.create_task(
            policy.check(f"https://load-{index}.synthetic.com/page", synthetic_fetcher)
        )
        for index in range(12)
    ]
    await asyncio.wait_for(both_started.wait(), timeout=2)
    await _wait_for(lambda: policy.inflight_origins == 2 and admission.queued_requests == 1)
    assert admission.active_requests == 1
    assert admission.queued_requests <= 1
    assert policy.inflight_origins <= 2
    release.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(*tasks, return_exceptions=True), timeout=3
    )
    assert sum(isinstance(value, RobotsPolicyError) for value in outcomes) >= 10
    assert admission.active_requests == 0
    assert admission.queued_requests == 0
    assert policy.inflight_origins == 0

    # The timed-out origin can retry, then a bounded volume forces eviction.
    await policy.check("https://load-0.synthetic.com/retry", synthetic_fetcher)
    for index in range(12, 24):
        await policy.check(f"https://load-{index}.synthetic.com/page", synthetic_fetcher)
        assert policy.cached_origins <= 3
        assert len(policy._cache) <= 3
    assert admission.active_requests == 0
    assert admission.queued_requests == 0
    assert policy.inflight_origins == 0


def test_default_robots_policies_and_cache_are_isolated_between_event_loops() -> None:
    policies: list[object] = []
    loop_refs: list[weakref.ReferenceType[asyncio.AbstractEventLoop]] = []

    def create_policy() -> None:
        async def get_policy() -> None:
            policy = get_default_robots_policy()
            policies.append(policy)
            loop_refs.append(weakref.ref(asyncio.get_running_loop()))

        asyncio.run(get_policy())

    create_policy()
    create_policy()
    assert policies[0] is not policies[1]
    assert policies[0]._cache == {}
    assert policies[1]._cache == {}
    gc.collect()
    assert all(loop_ref() is None for loop_ref in loop_refs)
