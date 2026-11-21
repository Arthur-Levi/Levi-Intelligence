"""Validated database connection limits for the crawler supervisor."""

import math
import os
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


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
    lock_timeout_seconds: float = 1.0
    statement_timeout_seconds: float = 5.0
    idle_transaction_timeout_seconds: float = 5.0
    command_timeout_seconds: float = 6.0
    transaction_timeout_seconds: float = 15.0

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
            lock_timeout_seconds=_seconds(
                "CRAWLER_DB_LOCK_TIMEOUT_SECONDS", 1, minimum=0.1, maximum=10
            ),
            statement_timeout_seconds=_seconds(
                "CRAWLER_DB_STATEMENT_TIMEOUT_SECONDS", 5, minimum=0.2, maximum=30
            ),
            idle_transaction_timeout_seconds=_seconds(
                "CRAWLER_DB_IDLE_TRANSACTION_TIMEOUT_SECONDS", 5, minimum=0.2, maximum=30
            ),
            command_timeout_seconds=_seconds(
                "CRAWLER_DB_COMMAND_TIMEOUT_SECONDS", 6, minimum=0.2, maximum=30
            ),
            transaction_timeout_seconds=_seconds(
                "CRAWLER_DB_TRANSACTION_TIMEOUT_SECONDS", 15, minimum=1, maximum=60
            ),
        )
        if resources.pool_size < resources.concurrency:
            raise ValueError("CRAWLER_DB_POOL_SIZE must cover worker concurrency")
        if not (
            resources.lock_timeout_seconds < resources.statement_timeout_seconds
            < resources.command_timeout_seconds < resources.transaction_timeout_seconds
        ):
            raise ValueError("crawler database timeout ordering is inconsistent")
        if resources.idle_transaction_timeout_seconds > resources.transaction_timeout_seconds:
            raise ValueError("idle transaction timeout exceeds transaction budget")
        return resources

    @property
    def server_settings(self) -> dict[str, str]:
        return {
            "statement_timeout": str(int(self.statement_timeout_seconds * 1000)),
            "lock_timeout": str(int(self.lock_timeout_seconds * 1000)),
            "idle_in_transaction_session_timeout": str(
                int(self.idle_transaction_timeout_seconds * 1000)
            ),
        }


def create_crawler_engine(
    database_url: str, resources: CrawlerDatabaseResources, worker_id: UUID
) -> AsyncEngine:
    """The sole database engine used by a crawler supervisor process."""
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=resources.pool_size,
        max_overflow=resources.max_overflow,
        pool_timeout=resources.pool_timeout_seconds,
        connect_args={
            "timeout": resources.connect_timeout_seconds,
            "command_timeout": resources.command_timeout_seconds,
            "server_settings": {
                **resources.server_settings,
                "application_name": f"levi_crawler_{worker_id.hex[:12]}",
            },
        },
    )
