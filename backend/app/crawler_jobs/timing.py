"""Validated crawler attempt and shutdown timing contract (all units seconds)."""

import math
import os
from dataclasses import dataclass


def _seconds(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a finite number of seconds") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be within {minimum}..{maximum} seconds")
    return value


@dataclass(frozen=True)
class CrawlerTiming:
    attempt_seconds: float = 60.0
    cancellation_seconds: float = 5.0
    shutdown_grace_seconds: float = 15.0
    shutdown_cleanup_seconds: float = 5.0
    poll_seconds: float = 1.0

    @classmethod
    def from_env(cls) -> "CrawlerTiming":
        timing = cls(
            attempt_seconds=_seconds("CRAWLER_ATTEMPT_SECONDS", 60, minimum=1, maximum=3600),
            cancellation_seconds=_seconds(
                "CRAWLER_CANCELLATION_SECONDS", 5, minimum=0.1, maximum=30
            ),
            shutdown_grace_seconds=_seconds(
                "CRAWLER_SHUTDOWN_GRACE_SECONDS", 15, minimum=0, maximum=300
            ),
            shutdown_cleanup_seconds=_seconds(
                "CRAWLER_SHUTDOWN_CLEANUP_SECONDS", 5, minimum=0.1, maximum=30
            ),
            poll_seconds=_seconds("CRAWLER_POLL_SECONDS", 1, minimum=0.2, maximum=10),
        )
        if timing.cancellation_seconds > timing.shutdown_cleanup_seconds:
            raise ValueError("CRAWLER_CANCELLATION_SECONDS exceeds shutdown cleanup budget")
        return timing
