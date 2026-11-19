from __future__ import annotations

import asyncio

import pytest

from app.intelligence.crawler.admission import (
    AdmissionConfig,
    AdmissionController,
    AdmissionRejectedError,
)


@pytest.mark.asyncio
async def test_enforces_global_and_per_host_limits_without_host_monopoly() -> None:
    admission = AdmissionController(
        AdmissionConfig(global_limit=2, per_host_limit=1, max_waiters=4, burst=4)
    )
    await admission.acquire("A.EXAMPLE.")
    await admission.acquire("b.example")
    assert admission.active_requests == 2
    assert admission.tracked_hosts == 2

    blocked_same = asyncio.create_task(admission.acquire("a.example"))
    other_host = asyncio.create_task(admission.acquire("c.example"))
    await asyncio.sleep(0)
    assert admission.queued_requests == 2
    await admission.release("a.example")
    await asyncio.wait_for(blocked_same, 0.2)
    assert admission.active_requests == 2
    await admission.release("a.example")
    await admission.release("b.example")
    await asyncio.wait_for(other_host, 0.2)
    await admission.release("c.example")
    assert admission.active_requests == 0


@pytest.mark.asyncio
async def test_queue_is_bounded_and_cancelled_waiter_is_removed() -> None:
    admission = AdmissionController(
        AdmissionConfig(global_limit=1, per_host_limit=1, max_waiters=1, burst=2)
    )
    await admission.acquire("one.example")
    first = asyncio.create_task(admission.acquire("one.example"))
    await asyncio.sleep(0)
    with pytest.raises(AdmissionRejectedError, match="queue is full"):
        await admission.acquire("two.example")
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert admission.queued_requests == 0
    await admission.release("one.example")
    assert admission.active_requests == 0


@pytest.mark.asyncio
async def test_rate_limit_uses_monotonic_clock_and_has_bounded_wait() -> None:
    now = [10.0]
    admission = AdmissionController(
        AdmissionConfig(
            global_limit=1,
            per_host_limit=1,
            max_waiters=2,
            requests_per_second=1,
            burst=1,
            max_wait_seconds=2,
        ),
        clock=lambda: now[0],
    )
    await admission.acquire("rate.example")
    await admission.release("rate.example")
    task = asyncio.create_task(admission.acquire("rate.example"))
    await asyncio.sleep(0)
    assert admission.queued_requests == 1
    now[0] += 1
    await admission.notify_clock_advanced()
    await asyncio.wait_for(task, 0.2)
    await admission.release("rate.example")


@pytest.mark.asyncio
async def test_host_table_is_bounded_and_rejects_when_active_states_fill_it() -> None:
    admission = AdmissionController(
        AdmissionConfig(global_limit=2, per_host_limit=1, max_hosts=2, burst=1)
    )
    await admission.acquire("one.example")
    await admission.acquire("two.example")
    with pytest.raises(AdmissionRejectedError, match="host-state capacity"):
        await admission.acquire("three.example")
    assert admission.tracked_hosts <= 2
    await admission.release("one.example")
    await admission.release("two.example")


@pytest.mark.asyncio
async def test_many_hostnames_do_not_grow_the_state_table_unbounded() -> None:
    now = [0.0]
    admission = AdmissionController(
        AdmissionConfig(global_limit=1, per_host_limit=1, max_hosts=3, burst=1),
        clock=lambda: now[0],
    )
    for index in range(100):
        now[0] += 1
        hostname = f"host-{index}.example.com"
        await admission.acquire(hostname)
        await admission.release(hostname)
        assert admission.tracked_hosts <= 3


@pytest.mark.asyncio
async def test_high_concurrency_stays_within_global_and_per_host_limits() -> None:
    admission = AdmissionController(
        AdmissionConfig(
            global_limit=8,
            per_host_limit=2,
            max_waiters=192,
            max_hosts=64,
            burst=200,
        )
    )
    active = 0
    peak = 0
    per_host_active: dict[str, int] = {}

    async def visit(index: int) -> None:
        nonlocal active, peak
        hostname = f"host-{index % 16}.example.com"
        async with admission.request(hostname):
            active += 1
            peak = max(peak, active)
            per_host_active[hostname] = per_host_active.get(hostname, 0) + 1
            assert per_host_active[hostname] <= 2
            await asyncio.sleep(0)
            per_host_active[hostname] -= 1
            active -= 1

    await asyncio.gather(*(visit(index) for index in range(200)))
    assert peak <= 8
    assert admission.active_requests == 0
    assert admission.queued_requests == 0


@pytest.mark.asyncio
async def test_wait_timeout_and_unmatched_release_fail_without_leaking_state() -> None:
    admission = AdmissionController(
        AdmissionConfig(
            global_limit=1,
            per_host_limit=1,
            max_waiters=1,
            max_wait_seconds=0.01,
            burst=2,
        )
    )
    await admission.acquire("held.example")
    with pytest.raises(AdmissionRejectedError, match="wait timed out"):
        await admission.acquire("held.example")
    assert admission.queued_requests == 0
    assert admission.active_requests == 1
    with pytest.raises(RuntimeError, match="without a matching acquisition"):
        await admission.release("other.example")
    assert admission.active_requests == 1
    await admission.release("held.example")
    assert admission.active_requests == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"global_limit": 0},
        {"per_host_limit": 0},
        {"max_waiters": 0},
        {"max_hosts": 0},
        {"burst": 0},
        {"requests_per_second": float("inf")},
        {"max_wait_seconds": 0},
        {"global_limit": True},
        {"requests_per_second": "1"},
    ],
)
def test_rejects_invalid_admission_config(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AdmissionConfig(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_exception_and_repeated_cancellation_release_capacity() -> None:
    admission = AdmissionController(AdmissionConfig(burst=20))
    with pytest.raises(RuntimeError):
        async with admission.request("exception.example"):
            raise RuntimeError("synthetic")
    assert admission.active_requests == 0

    entered = asyncio.Event()
    never = asyncio.Event()

    async def hold() -> None:
        async with admission.request("cancel.example"):
            entered.set()
            await never.wait()

    task = asyncio.create_task(hold())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert admission.active_requests == 0


@pytest.mark.asyncio
async def test_second_cancellation_during_release_does_not_leak_capacity() -> None:
    class SlowReleaseAdmission(AdmissionController):
        def __init__(self) -> None:
            super().__init__(AdmissionConfig(burst=4))
            self.release_started = asyncio.Event()
            self.finish_release = asyncio.Event()

        async def release(self, hostname: str) -> None:
            self.release_started.set()
            await self.finish_release.wait()
            await super().release(hostname)

    admission = SlowReleaseAdmission()
    entered = asyncio.Event()

    async def hold() -> None:
        async with admission.request("repeat-cancel.example"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await entered.wait()
    task.cancel()
    await admission.release_started.wait()
    task.cancel()
    admission.finish_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert admission.active_requests == 0
