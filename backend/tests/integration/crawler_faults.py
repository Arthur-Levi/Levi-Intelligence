"""Test-only, one-shot fault injection at crawler transaction boundaries."""

from collections.abc import Callable
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawler_jobs.postgres import PostgresCrawlerJobRepository
from app.models.prospecting import CrawlerJob, CrawlerJobAttempt, CrawlerJobStatus, SourceRecord


class FaultPoint(StrEnum):
    BEFORE_CLAIM = "before_claim"
    AFTER_CLAIM_COMMIT = "after_claim_commit"
    AFTER_ATTEMPT_CREATION = "after_attempt_creation"
    BEFORE_RENEWAL = "before_renewal"
    BEFORE_RESULT = "before_result"
    DURING_RESULT = "during_result"
    BEFORE_COMMIT = "before_commit"
    AMBIGUOUS_RESULT_COMMIT = "ambiguous_result_commit"
    DURING_RETRY = "during_retry"
    DURING_CANCELLATION = "during_cancellation"


class InjectedFault(RuntimeError):
    """A deterministic test fault, never used by production code."""


FIXTURE_NAMESPACE = UUID("ecefbf40-bc56-447a-8d02-d8c5239744a9")


def fixture_id(seed: str, label: str) -> UUID:
    """Stable synthetic identifier so a failing scenario can be replayed."""
    return uuid5(FIXTURE_NAMESPACE, f"{seed}:{label}")


class FaultPlan:
    def __init__(self, point: FaultPoint, *, occurrence: int = 1) -> None:
        if occurrence < 1:
            raise ValueError("occurrence must be positive")
        self.point = point
        self.occurrence = occurrence
        self.seen = 0
        self.fired = False

    def hit(self, point: FaultPoint) -> None:
        if point != self.point:
            return
        self.seen += 1
        if self.seen == self.occurrence:
            self.fired = True
            raise InjectedFault(point.value)

    def verify(self) -> None:
        if not self.fired:
            raise AssertionError(f"fault never reached {self.point.value}: saw {self.seen}")


class FaultSessionFactory:
    """Installs listeners on each disposable session, not on global Session state."""

    def __init__(self, factory: Callable[[], AsyncSession], plan: FaultPlan) -> None:
        self.factory = factory
        self.plan = plan

    def __call__(self) -> AsyncSession:
        session = self.factory()
        sync = session.sync_session
        plan = self.plan

        def before_flush(s: Any, *_: Any) -> None:
            if any(isinstance(row, CrawlerJobAttempt) for row in s.new):
                s.info["claim_attempt"] = True
            if any(isinstance(row, SourceRecord) for row in s.new):
                s.info["result"] = True
                plan.hit(FaultPoint.DURING_RESULT)
            for row in s.dirty:
                if not isinstance(row, CrawlerJob):
                    continue
                if row.status == CrawlerJobStatus.QUEUED and row.next_attempt_at is not None:
                    plan.hit(FaultPoint.DURING_RETRY)
                if row.status == CrawlerJobStatus.CANCELLED:
                    plan.hit(FaultPoint.DURING_CANCELLATION)

        def after_flush(s: Any, *_: Any) -> None:
            if s.info.get("claim_attempt"):
                plan.hit(FaultPoint.AFTER_ATTEMPT_CREATION)

        def before_commit(s: Any) -> None:
            # This listener runs before SQLAlchemy's commit flush. It is a true
            # pre-commit failure, even when the pending ORM rows are not flushed.
            if s.new or s.dirty or s.info.get("claim_attempt") or s.info.get("result"):
                plan.hit(FaultPoint.BEFORE_COMMIT)

        def after_commit(s: Any) -> None:
            if s.info.get("claim_attempt"):
                plan.hit(FaultPoint.AFTER_CLAIM_COMMIT)
            if s.info.get("result"):
                # The server committed; only the client-facing acknowledgment
                # is made ambiguous. A real wire loss needs a separate test.
                plan.hit(FaultPoint.AMBIGUOUS_RESULT_COMMIT)

        event.listen(sync, "before_flush", before_flush)
        event.listen(sync, "after_flush", after_flush)
        event.listen(sync, "before_commit", before_commit)
        event.listen(sync, "after_commit", after_commit)
        return session


class FaultRepository(PostgresCrawlerJobRepository):
    def __init__(self, factory: Callable[[], AsyncSession], plan: FaultPlan) -> None:
        super().__init__(FaultSessionFactory(factory, plan))  # type: ignore[arg-type]
        self.plan = plan

    async def claim_next(self, *args: Any, **kwargs: Any) -> Any:
        self.plan.hit(FaultPoint.BEFORE_CLAIM)
        return await super().claim_next(*args, **kwargs)

    async def heartbeat(self, job: Any) -> bool:
        self.plan.hit(FaultPoint.BEFORE_RENEWAL)
        return await super().heartbeat(job)

    async def persist_result(self, job: Any, result: Any) -> None:
        self.plan.hit(FaultPoint.BEFORE_RESULT)
        await super().persist_result(job, result)
