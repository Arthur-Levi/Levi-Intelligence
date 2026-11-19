"""Conservative robots.txt policy parsing and bounded process-local caching."""

from __future__ import annotations

import asyncio
import math
import re
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .exceptions import CrawlerSecurityError, RobotsDeniedError
from .url_validator import ValidatedURL, validate_url

if TYPE_CHECKING:
    from .http_transport import FetchResponse

CRAWLER_USER_AGENT = "LeviIntelligenceBot"


class RobotsPolicyError(CrawlerSecurityError):
    """Raised when robots policy cannot be fetched or safely interpreted."""


class RobotsFormatError(ValueError):
    """Raised for malformed robots directives that cannot be interpreted safely."""


_PERCENT_ESCAPE = re.compile(r"%([0-9a-fA-F]{2})")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


def _normalize_percent_encoding(value: str) -> str:
    """Decode percent-encoded unreserved octets and uppercase other escapes."""

    def normalize(match: re.Match[str]) -> str:
        octet = int(match.group(1), 16)
        char = chr(octet)
        return char if char in _UNRESERVED else f"%{octet:02X}"

    return _PERCENT_ESCAPE.sub(normalize, value)


@dataclass(frozen=True, slots=True)
class _Rule:
    allow: bool
    pattern: str

    @property
    def specificity(self) -> int:
        normalized = _normalize_percent_encoding(self.pattern)
        return len(normalized.removesuffix("$").replace("*", "").encode("utf-8"))

    def matches(self, path: str) -> bool:
        if not self.pattern:
            return False
        normalized = _normalize_percent_encoding(self.pattern)
        end_anchored = normalized.endswith("$")
        pattern = normalized[:-1] if end_anchored else normalized
        regex = "^" + ".*".join(re.escape(part) for part in pattern.split("*"))
        regex += "$" if end_anchored else ".*"
        return re.match(regex, path) is not None


@dataclass(frozen=True, slots=True)
class _Group:
    agents: tuple[str, ...]
    rules: tuple[_Rule, ...]


@dataclass(frozen=True, slots=True)
class RobotsRules:
    """Compiled rules for a robots policy."""

    groups: tuple[_Group, ...]
    deny_all: bool = False

    @classmethod
    def allow_all(cls) -> RobotsRules:
        return cls(())

    @classmethod
    def disallow_all(cls) -> RobotsRules:
        return cls((), deny_all=True)

    @classmethod
    def parse(cls, body: bytes, *, user_agent: str) -> RobotsRules:
        try:
            text = body.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise RobotsFormatError("robots.txt is not valid UTF-8") from exc

        parsed_groups: list[_Group] = []
        agents: list[str] = []
        rules: list[_Rule] = []
        saw_rule = False

        def flush() -> None:
            nonlocal agents, rules, saw_rule
            if agents:
                parsed_groups.append(_Group(tuple(agents), tuple(rules)))
            agents, rules, saw_rule = [], [], False

        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            if ":" not in line:
                raise RobotsFormatError("robots.txt contains a malformed directive")
            name, value = (part.strip() for part in line.split(":", 1))
            directive = name.lower()
            if directive == "user-agent":
                if not value:
                    raise RobotsFormatError("robots.txt contains an empty user-agent")
                if saw_rule:
                    flush()
                agents.append(value.lower())
            elif directive in {"allow", "disallow"}:
                if not agents:
                    # RFC parsers ignore rules outside a user-agent group. The
                    # unknown placement is safer treated as malformed here.
                    raise RobotsFormatError("robots rule appears before user-agent")
                saw_rule = True
                # An empty Disallow means no restriction; empty Allow is inert.
                if value:
                    rules.append(_Rule(allow=directive == "allow", pattern=value))
            else:
                # RFC 9309 permits extension records; they do not change access.
                continue
        flush()

        product = user_agent.split("/", 1)[0].split(None, 1)[0].lower()
        scored: list[tuple[int, _Group]] = []
        for group in parsed_groups:
            score = max(
                (len(agent) for agent in group.agents if agent != "*" and agent in product),
                default=-1,
            )
            if score >= 0:
                scored.append((score, group))
        if scored:
            best = max(score for score, _ in scored)
            selected = tuple(group for score, group in scored if score == best)
        else:
            selected = tuple(group for group in parsed_groups if "*" in group.agents)
        return cls(selected)

    def allows(self, url: str) -> bool:
        if self.deny_all:
            return False
        parsed = urlsplit(url)
        path = _normalize_percent_encoding(parsed.path or "/")
        if parsed.query:
            path += "?" + _normalize_percent_encoding(parsed.query)
        matches = [rule for group in self.groups for rule in group.rules if rule.matches(path)]
        if not matches:
            return True
        longest = max(rule.specificity for rule in matches)
        # RFC 9309 preference: Allow wins ties of equal specific length.
        return any(rule.allow for rule in matches if rule.specificity == longest)


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    rules: RobotsRules


@dataclass(slots=True)
class _Flight:
    future: asyncio.Future[tuple[RobotsRules | None, BaseException | None] | None]
    waiters: int


RobotsFetcher = Callable[[str], Awaitable["FetchResponse"]]


class RobotsPolicy:
    """Robots policy with bounded TTL cache and per-origin single-flight fetches."""

    def __init__(
        self,
        *,
        user_agent: str = CRAWLER_USER_AGENT,
        ttl_seconds: float = 3_600.0,
        max_entries: int = 1_024,
        max_inflight_origins: int = 128,
        max_waiters_per_origin: int = 64,
        max_bytes: int = 500_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if user_agent != CRAWLER_USER_AGENT:
            raise ValueError(f"user_agent must be {CRAWLER_USER_AGENT}")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (float, int))
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be finite and greater than zero")
        for name, value in (
            ("max_entries", max_entries),
            ("max_inflight_origins", max_inflight_origins),
            ("max_waiters_per_origin", max_waiters_per_origin),
            ("max_bytes", max_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        self.user_agent = user_agent
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.max_inflight_origins = max_inflight_origins
        self.max_waiters_per_origin = max_waiters_per_origin
        self.max_bytes = max_bytes
        self._clock = clock
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._inflight: dict[str, _Flight] = {}
        self._lock = asyncio.Lock()
        self._pending_checks = 0

    @property
    def cached_origins(self) -> int:
        return len(self._cache)

    @property
    def inflight_origins(self) -> int:
        return len(self._inflight)

    @property
    def pending_checks(self) -> int:
        """Number of checks currently executing or waiting for policy state."""
        return self._pending_checks

    @property
    def max_pending_checks(self) -> int:
        """Finite bound covering every inflight origin and its allowed waiters."""
        return self.max_inflight_origins * (self.max_waiters_per_origin + 1)

    def _origin_and_robots_url(self, url: str) -> tuple[str, str]:
        target: ValidatedURL = validate_url(url)
        origin = f"{target.scheme}://{target.hostname}"
        default_port = 443 if target.scheme == "https" else 80
        if target.port != default_port:
            origin += f":{target.port}"
        return origin, f"{origin}/robots.txt"

    def _interpret(self, response: FetchResponse) -> RobotsRules:
        if len(response.body) > self.max_bytes:
            raise RobotsPolicyError("robots.txt exceeds the configured byte limit")
        if response.status == 200:
            try:
                return RobotsRules.parse(response.body, user_agent=self.user_agent)
            except RobotsFormatError:
                # A malformed policy must never turn into permission.
                return RobotsRules.disallow_all()
        if response.status == 404:
            return RobotsRules.allow_all()
        if response.status in {401, 403}:
            return RobotsRules.disallow_all()
        if response.status >= 500:
            raise RobotsPolicyError(f"robots.txt server returned HTTP {response.status}")
        raise RobotsPolicyError(f"robots.txt returned unexpected HTTP {response.status}")

    async def _get_rules(self, origin: str, robots_url: str, fetcher: RobotsFetcher) -> RobotsRules:
        while True:
            async with self._lock:
                now = self._clock()
                for cached_origin, cached_entry in tuple(self._cache.items()):
                    if cached_entry.expires_at <= now:
                        del self._cache[cached_origin]
                entry = self._cache.get(origin)
                if entry is not None and entry.expires_at > now:
                    self._cache.move_to_end(origin)
                    return entry.rules
                self._cache.pop(origin, None)

                flight = self._inflight.get(origin)
                owner = flight is None
                if owner:
                    if len(self._inflight) >= self.max_inflight_origins:
                        raise RobotsPolicyError("robots policy fetch capacity is full")
                    flight = _Flight(asyncio.get_running_loop().create_future(), waiters=0)
                    self._inflight[origin] = flight
                else:
                    assert flight is not None
                    if flight.waiters >= self.max_waiters_per_origin:
                        raise RobotsPolicyError("too many requests waiting for robots policy")
                    flight.waiters += 1

            assert flight is not None
            if not owner:
                try:
                    result = await asyncio.shield(flight.future)
                finally:
                    async with self._lock:
                        flight.waiters = max(0, flight.waiters - 1)
                if result is None:
                    continue
                rules, error = result
                if error is not None:
                    raise error
                assert rules is not None
                return rules

            try:
                response = await fetcher(robots_url)
                rules = self._interpret(response)
            except asyncio.CancelledError:
                async with self._lock:
                    if self._inflight.get(origin) is flight:
                        del self._inflight[origin]
                    if not flight.future.done():
                        flight.future.set_result(None)
                raise
            except Exception as exc:
                error = exc if isinstance(exc, RobotsPolicyError) else RobotsPolicyError(
                    "robots.txt could not be retrieved safely"
                )
                if error is not exc:
                    error.__cause__ = exc
                async with self._lock:
                    if self._inflight.get(origin) is flight:
                        del self._inflight[origin]
                    if not flight.future.done():
                        flight.future.set_result((None, error))
                if error is exc:
                    raise
                raise error from exc

            async with self._lock:
                if self._inflight.get(origin) is flight:
                    del self._inflight[origin]
                self._cache[origin] = _CacheEntry(self._clock() + self.ttl_seconds, rules)
                self._cache.move_to_end(origin)
                while len(self._cache) > self.max_entries:
                    self._cache.popitem(last=False)
                if not flight.future.done():
                    flight.future.set_result((rules, None))
            return rules

    async def check(self, url: str, fetcher: RobotsFetcher) -> None:
        origin, robots_url = self._origin_and_robots_url(url)
        # Count before the first await so callers cannot accumulate without
        # bound while queued on the policy lock itself.
        if self._pending_checks >= self.max_pending_checks:
            raise RobotsPolicyError("robots policy check capacity is full")
        self._pending_checks += 1
        try:
            try:
                rules = await self._get_rules(origin, robots_url, fetcher)
            except asyncio.CancelledError:
                raise
            except RobotsPolicyError:
                raise
            except CrawlerSecurityError as exc:
                raise RobotsPolicyError("robots.txt request failed closed") from exc
            if not rules.allows(url):
                raise RobotsDeniedError("robots.txt disallows this crawler path")
        finally:
            self._pending_checks -= 1


DEFAULT_ROBOTS_POLICY = RobotsPolicy()

_DEFAULT_POLICIES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, RobotsPolicy] = (
    weakref.WeakKeyDictionary()
)
_DEFAULT_POLICIES_LOCK = threading.Lock()


def get_default_robots_policy() -> RobotsPolicy:
    """Return the shared bounded policy cache for the current worker event loop."""
    loop = asyncio.get_running_loop()
    with _DEFAULT_POLICIES_LOCK:
        policy = _DEFAULT_POLICIES.get(loop)
        if policy is None:
            policy = RobotsPolicy()
            _DEFAULT_POLICIES[loop] = policy
        return policy
