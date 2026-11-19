"""Bounded, process-local admission control for crawler HTTP requests."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from .exceptions import CrawlerSecurityError


class AdmissionRejectedError(CrawlerSecurityError):
    """Raised when the bounded admission queue or host-state table is full."""


@dataclass(frozen=True, slots=True)
class AdmissionConfig:
    """Process-local concurrency, queue, host-state, and rate limits."""

    global_limit: int = 16
    per_host_limit: int = 2
    max_waiters: int = 64
    max_hosts: int = 2_048
    requests_per_second: float = 1.0
    burst: int = 2
    max_wait_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in ("global_limit", "per_host_limit", "max_waiters", "max_hosts", "burst"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.per_host_limit > self.global_limit:
            raise ValueError("per_host_limit cannot exceed global_limit")
        if (
            isinstance(self.requests_per_second, bool)
            or not isinstance(self.requests_per_second, (float, int))
            or not math.isfinite(self.requests_per_second)
            or self.requests_per_second <= 0
        ):
            raise ValueError("requests_per_second must be finite and greater than zero")
        if (
            isinstance(self.max_wait_seconds, bool)
            or not isinstance(self.max_wait_seconds, (float, int))
            or not math.isfinite(self.max_wait_seconds)
            or self.max_wait_seconds <= 0
        ):
            raise ValueError("max_wait_seconds must be finite and greater than zero")


@dataclass(slots=True)
class _HostState:
    active: int
    waiters: int
    tokens: float
    updated_at: float
    last_used: float


@dataclass(frozen=True, slots=True)
class _Waiter:
    hostname: str


class AdmissionController:
    """Fair bounded admission with per-host token-bucket rate limiting.

    Instances coordinate tasks within one Python process only. A finite host
    table fails closed when it cannot safely evict an inactive, replenished entry.
    """

    def __init__(
        self,
        config: AdmissionConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or AdmissionConfig()
        self._clock = clock
        self._condition = asyncio.Condition()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_lock = threading.Lock()
        self._states: dict[str, _HostState] = {}
        self._waiters: deque[_Waiter] = deque()
        self._active = 0
        self._pending_acquires = 0

    def _bind_to_running_loop(self) -> None:
        loop = asyncio.get_running_loop()
        with self._loop_lock:
            if self._loop is None:
                self._loop = loop
            elif self._loop is not loop:
                raise RuntimeError("AdmissionController cannot be shared across event loops")

    @property
    def active_requests(self) -> int:
        return self._active

    @property
    def queued_requests(self) -> int:
        return len(self._waiters)

    @property
    def tracked_hosts(self) -> int:
        return len(self._states)

    @property
    def pending_acquires(self) -> int:
        """Number of active or waiting calls currently entering admission."""
        return self._pending_acquires

    @property
    def max_pending_acquires(self) -> int:
        """Finite bound covering active capacity and the explicit waiter queue."""
        return self.config.global_limit + self.config.max_waiters

    @staticmethod
    def normalize_hostname(hostname: str) -> str:
        """Normalize a DNS hostname to a stable lower-case IDNA key."""
        try:
            normalized = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("hostname is not valid IDNA") from exc
        if not normalized or len(normalized) > 253:
            raise ValueError("hostname is empty or too long")
        return normalized

    def _refill(self, state: _HostState, now: float) -> None:
        elapsed = max(0.0, now - state.updated_at)
        state.tokens = min(
            float(self.config.burst),
            state.tokens + elapsed * self.config.requests_per_second,
        )
        state.updated_at = now

    def _prune(self, now: float) -> None:
        for hostname, state in tuple(self._states.items()):
            self._refill(state, now)
            if state.active == 0 and state.waiters == 0 and state.tokens >= self.config.burst:
                del self._states[hostname]

    def _state_for(self, hostname: str, now: float) -> _HostState:
        state = self._states.get(hostname)
        if state is not None:
            self._refill(state, now)
            state.last_used = now
            return state

        self._prune(now)
        if len(self._states) >= self.config.max_hosts:
            candidates = [
                (entry.last_used, key)
                for key, entry in self._states.items()
                if entry.active == 0 and entry.waiters == 0 and entry.tokens >= self.config.burst
            ]
            if candidates:
                del self._states[min(candidates)[1]]
        if len(self._states) >= self.config.max_hosts:
            raise AdmissionRejectedError("Crawler host-state capacity is full")

        state = _HostState(
            active=0,
            waiters=0,
            tokens=float(self.config.burst),
            updated_at=now,
            last_used=now,
        )
        self._states[hostname] = state
        return state

    def _first_eligible(self, now: float) -> _Waiter | None:
        if self._active >= self.config.global_limit:
            return None
        for waiter in self._waiters:
            state = self._states[waiter.hostname]
            self._refill(state, now)
            if state.active < self.config.per_host_limit and state.tokens >= 1.0:
                return waiter
        return None

    def _next_rate_delay(self, now: float) -> float | None:
        if self._active >= self.config.global_limit:
            return None
        delays = []
        for waiter in self._waiters:
            state = self._states[waiter.hostname]
            self._refill(state, now)
            if state.active < self.config.per_host_limit and state.tokens < 1.0:
                delays.append((1.0 - state.tokens) / self.config.requests_per_second)
        return min(delays) if delays else None

    async def acquire(self, hostname: str) -> None:
        self._bind_to_running_loop()
        key = self.normalize_hostname(hostname)
        # Count before the first await so contention on Condition's own lock
        # cannot create a hidden, unbounded queue ahead of max_waiters.
        if self._pending_acquires >= self.max_pending_acquires:
            raise AdmissionRejectedError("Crawler admission pending capacity is full")
        self._pending_acquires += 1
        try:
            await self._acquire_pending(key)
        finally:
            self._pending_acquires -= 1

    async def _acquire_pending(self, key: str) -> None:
        async with self._condition:
            now = self._clock()
            state = self._state_for(key, now)
            # Do not let a new arrival jump ahead of already queued work.
            if not self._waiters and self._active < self.config.global_limit:
                self._refill(state, now)
                if state.active < self.config.per_host_limit and state.tokens >= 1.0:
                    state.tokens -= 1.0
                    state.active += 1
                    self._active += 1
                    return

            if len(self._waiters) >= self.config.max_waiters:
                self._prune(now)
                if state.active == 0 and state.waiters == 0 and state.tokens >= self.config.burst:
                    self._states.pop(key, None)
                raise AdmissionRejectedError("Crawler admission queue is full")

            waiter = _Waiter(key)
            self._waiters.append(waiter)
            state.waiters += 1
            deadline = now + self.config.max_wait_seconds
            try:
                while True:
                    now = self._clock()
                    remaining = deadline - now
                    if remaining <= 0:
                        raise AdmissionRejectedError("Crawler admission wait timed out")
                    eligible = self._first_eligible(now)
                    if eligible is waiter:
                        self._waiters.remove(waiter)
                        state = self._states[key]
                        state.waiters -= 1
                        state.tokens -= 1.0
                        state.active += 1
                        state.last_used = now
                        self._active += 1
                        self._condition.notify_all()
                        return

                    delay = self._next_rate_delay(now)
                    try:
                        timeout = remaining if delay is None else min(remaining, max(delay, 0.001))
                        await asyncio.wait_for(self._condition.wait(), timeout=timeout)
                    except TimeoutError:
                        if self._clock() >= deadline:
                            raise AdmissionRejectedError(
                                "Crawler admission wait timed out"
                            ) from None
            except BaseException:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                    current = self._states.get(key)
                    if current is not None:
                        current.waiters -= 1
                        current.last_used = self._clock()
                        self._prune(current.last_used)
                self._condition.notify_all()
                raise

    async def release(self, hostname: str) -> None:
        self._bind_to_running_loop()
        key = self.normalize_hostname(hostname)
        async with self._condition:
            state = self._states.get(key)
            if state is None or state.active <= 0 or self._active <= 0:
                raise RuntimeError("crawler admission release without a matching acquisition")
            state.active -= 1
            state.last_used = self._clock()
            self._active -= 1
            self._prune(state.last_used)
            self._condition.notify_all()

    async def notify_clock_advanced(self) -> None:
        """Wake queued tasks after an injected test clock advances."""
        self._bind_to_running_loop()
        async with self._condition:
            self._condition.notify_all()

    @asynccontextmanager
    async def request(self, hostname: str) -> AsyncIterator[None]:
        key = self.normalize_hostname(hostname)
        await self.acquire(key)
        try:
            yield
        finally:
            release_task = asyncio.create_task(self.release(key))
            cancelled_during_release = False
            while not release_task.done():
                try:
                    await asyncio.shield(release_task)
                except asyncio.CancelledError:
                    cancelled_during_release = True
            release_task.result()
            if cancelled_during_release:
                raise asyncio.CancelledError


def get_default_admission_controller() -> AdmissionController:
    """Return the shared process-local controller for the current event loop.

    Separate event loops receive separate controllers because asyncio
    synchronization primitives cannot safely coordinate across loops or threads.
    """
    loop = asyncio.get_running_loop()
    loop_attributes = vars(loop)
    controller = loop_attributes.get("_levi_crawler_admission_controller")
    if controller is None:
        controller = AdmissionController()
        loop_attributes["_levi_crawler_admission_controller"] = controller
    return controller
