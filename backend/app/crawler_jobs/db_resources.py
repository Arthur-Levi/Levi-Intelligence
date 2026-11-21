"""Validated database connection limits for the crawler supervisor."""

import math
import os
from dataclasses import dataclass


def _integer(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be within {minimum}..{maximum}")
    return value


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
class CrawlerDatabaseResources:
    concurrency: int = 2
    pool_size: int = 2
    max_overflow: int = 0
    pool_timeout_seconds: float = 2.0
    connect_timeout_seconds: float = 3.0

    @classmethod
    def from_env(cls) -> "CrawlerDatabaseResources":
        concurrency = _integer("CRAWLER_WORKER_CONCURRENCY", 2, minimum=1, maximum=8)
        pool_size = _integer("CRAWLER_DB_POOL_SIZE", concurrency, minimum=1, maximum=8)
        overflow = _integer("CRAWLER_DB_MAX_OVERFLOW", 0, minimum=0, maximum=0)
        resources = cls(
            concurrency=concurrency,
            pool_size=pool_size,
            max_overflow=overflow,
            pool_timeout_seconds=_seconds(
                "CRAWLER_DB_POOL_TIMEOUT_SECONDS", 2, minimum=0.1, maximum=5
            ),
            connect_timeout_seconds=_seconds(
                "CRAWLER_DB_CONNECT_TIMEOUT_SECONDS", 3, minimum=0.1, maximum=5
            ),
        )
        if resources.pool_size < resources.concurrency:
            raise ValueError("CRAWLER_DB_POOL_SIZE must cover worker concurrency")
        return resources
