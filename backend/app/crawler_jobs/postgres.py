"""Narrow PostgreSQL adapter for the crawler job lifecycle."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crawler_jobs.contracts import CrawlerJobInput, CrawlerJobResult
from app.crawler_jobs.events import emit
from app.models.prospecting import (
    Company,
    CrawlerJob,
    CrawlerJobAttempt,
    CrawlerJobStatus,
    SourceRecord,
)

MAX_ATTEMPTS = 5
LEASE_SECONDS = 90
GLOBAL_QUEUED_LIMIT = 10_000
TENANT_QUEUED_LIMIT = 100
RECOVERY_BATCH = 32
PENDING_SCAN_DELAY_SECONDS = 1


class CrawlerDatabaseError(RuntimeError):
    """Sanitized database failure; attempt ownership must be reconciled."""


class PostgresCrawlerJobRepository:
    """Exposes only crawler lifecycle operations; each write has its own transaction."""

    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, transaction_timeout_seconds: float = 15
    ) -> None:
        self._factory = factory
        self._transaction_timeout_seconds = transaction_timeout_seconds

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        try:
            async with asyncio.timeout(self._transaction_timeout_seconds):
                async with self._factory() as session, session.begin():
                    yield session
        except (SQLAlchemyError, TimeoutError) as exc:
            raise CrawlerDatabaseError("crawler database transaction failed") from exc

    @asynccontextmanager
    async def _read_session(self) -> AsyncIterator[AsyncSession]:
        try:
            async with asyncio.timeout(self._transaction_timeout_seconds):
                async with self._factory() as session:
                    yield session
        except (SQLAlchemyError, TimeoutError) as exc:
            raise CrawlerDatabaseError("crawler database read failed") from exc

    @staticmethod
    def _payload(
        row: CrawlerJob, token: UUID, attempt_id: UUID, worker_id: UUID
    ) -> CrawlerJobInput:
        return CrawlerJobInput(
            job_id=row.id,
            tenant_id=row.tenant_id,
            company_id=row.company_id,
            website_url=row.website_url,
            idempotency_key=row.idempotency_key,
            lease_token=token,
            attempt_id=attempt_id,
            worker_id=worker_id,
        )

    @staticmethod
    async def _attempt(session: AsyncSession, row: CrawlerJob) -> CrawlerJobAttempt | None:
        return await session.scalar(
            select(CrawlerJobAttempt).where(
                CrawlerJobAttempt.tenant_id == row.tenant_id,
                CrawlerJobAttempt.job_id == row.id,
                CrawlerJobAttempt.generation == row.attempts,
            )
        )

    async def claim_next(
        self, tenant_id: UUID | None = None, *, worker_id: UUID | None = None
    ) -> CrawlerJobInput | None:
        async with self._transaction() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(681204117)"))
            now = datetime.now(UTC)
            # Expired attempts are retried with bounded exponential backoff or dead-lettered.
            expired = (CrawlerJob.status == CrawlerJobStatus.RUNNING) & (
                CrawlerJob.lease_expires_at <= now
            ) & or_(
                CrawlerJob.lease_token.is_not(None),
                CrawlerJob.next_attempt_at <= now,
            )
            rows = await session.scalars(
                select(CrawlerJob).where(expired)
                .order_by(CrawlerJob.next_attempt_at, CrawlerJob.created_at, CrawlerJob.id)
                .with_for_update(skip_locked=True).limit(RECOVERY_BATCH)
            )
            queued_global = await session.scalar(
                select(func.count()).select_from(CrawlerJob).where(
                    CrawlerJob.status == CrawlerJobStatus.QUEUED
                )
            ) or 0
            queued_by_tenant: dict[UUID, int] = {}
            for row in rows:
                attempt = await self._attempt(session, row)
                if attempt is not None and attempt.ended_at is None:
                    attempt.ended_at = now
                    attempt.outcome = "expired"
                    emit(
                        "lease_expiration",
                        tenant_id=row.tenant_id,
                        job_id=row.id,
                        attempt_id=attempt.id,
                        worker_id=attempt.worker_id,
                        outcome="expired",
                    )
                row.lease_token = None
                if row.cancel_requested_at is not None:
                    row.status = CrawlerJobStatus.CANCELLED
                    row.lease_expires_at = None
                    row.finished_at = now
                    if attempt is not None:
                        attempt.outcome = "cancelled"
                    emit(
                        "recovery", tenant_id=row.tenant_id, job_id=row.id,
                        attempt_id=attempt.id if attempt else None,
                        worker_id=attempt.worker_id if attempt else None,
                        outcome="cancelled",
                    )
                elif row.attempts >= MAX_ATTEMPTS:
                    row.status = CrawlerJobStatus.FAILED
                    row.lease_expires_at = None
                    row.failure_code = "retry_exhausted"
                    row.finished_at = now
                    if attempt is not None:
                        attempt.outcome = "retry_exhausted"
                        attempt.failure_code = "retry_exhausted"
                    emit(
                        "recovery",
                        tenant_id=row.tenant_id,
                        job_id=row.id,
                        attempt_id=attempt.id if attempt else None,
                        worker_id=attempt.worker_id if attempt else None,
                        outcome="retry_exhausted",
                    )
                else:
                    if row.tenant_id not in queued_by_tenant:
                        queued_by_tenant[row.tenant_id] = await session.scalar(
                            select(func.count()).select_from(CrawlerJob).where(
                                CrawlerJob.tenant_id == row.tenant_id,
                                CrawlerJob.status == CrawlerJobStatus.QUEUED,
                            )
                        ) or 0
                    if (row.next_attempt_at is None or row.started_at is None or
                            row.next_attempt_at <= row.started_at):
                        row.next_attempt_at = now + timedelta(
                            seconds=min(60, 2**row.attempts)
                        )
                    if (queued_global >= GLOBAL_QUEUED_LIMIT or
                            queued_by_tenant[row.tenant_id] >= TENANT_QUEUED_LIMIT):
                        # RUNNING with an expired timestamp and no token is durable,
                        # unowned recovery pending. Keep its backoff deadline stable.
                        row.next_attempt_at = max(
                            row.next_attempt_at,
                            now + timedelta(seconds=PENDING_SCAN_DELAY_SECONDS),
                        )
                        continue
                    row.status = CrawlerJobStatus.QUEUED
                    row.lease_expires_at = None
                    queued_global += 1
                    queued_by_tenant[row.tenant_id] += 1
                    emit(
                        "recovery", tenant_id=row.tenant_id, job_id=row.id,
                        attempt_id=attempt.id if attempt else None,
                        worker_id=attempt.worker_id if attempt else None,
                        outcome="queued",
                    )
                    emit(
                        "retry_scheduled", tenant_id=row.tenant_id, job_id=row.id,
                        attempt_id=attempt.id if attempt else None,
                        worker_id=attempt.worker_id if attempt else None,
                        outcome="queued",
                    )

            global_running = await session.scalar(
                select(func.count())
                .select_from(CrawlerJob)
                .where(
                    CrawlerJob.status == CrawlerJobStatus.RUNNING,
                    CrawlerJob.lease_expires_at > now,
                )
            )
            if (global_running or 0) >= 8:
                return None
            candidate_query = (
                select(CrawlerJob)
                .where(
                    CrawlerJob.status == CrawlerJobStatus.QUEUED,
                    CrawlerJob.cancel_requested_at.is_(None),
                    or_(CrawlerJob.next_attempt_at.is_(None), CrawlerJob.next_attempt_at <= now),
                    CrawlerJob.attempts < MAX_ATTEMPTS,
                )
                .order_by(CrawlerJob.created_at, CrawlerJob.tenant_id)
                .with_for_update(skip_locked=True)
                .limit(64)
            )
            if tenant_id is not None:
                candidate_query = candidate_query.where(CrawlerJob.tenant_id == tenant_id)
            candidates = await session.scalars(candidate_query)
            selected: CrawlerJob | None = None
            for candidate in candidates:
                tenant_running = await session.scalar(
                    select(func.count())
                    .select_from(CrawlerJob)
                    .where(
                        CrawlerJob.tenant_id == candidate.tenant_id,
                        CrawlerJob.status == CrawlerJobStatus.RUNNING,
                        CrawlerJob.lease_expires_at > now,
                    )
                )
                if (tenant_running or 0) < 2:
                    selected = candidate
                    break
            if selected is None:
                return None
            token = uuid4()
            selected.status = CrawlerJobStatus.RUNNING
            selected.attempts += 1
            selected.started_at = now
            selected.heartbeat_at = now
            selected.lease_token = token
            selected.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
            identity = worker_id or uuid4()
            attempt = CrawlerJobAttempt(
                tenant_id=selected.tenant_id,
                job_id=selected.id,
                generation=selected.attempts,
                worker_id=identity,
                claimed_at=now,
            )
            session.add(attempt)
            await session.flush()
            emit(
                "claim",
                tenant_id=selected.tenant_id,
                job_id=selected.id,
                attempt_id=attempt.id,
                worker_id=identity,
                outcome="running",
            )
            return self._payload(selected, token, attempt.id, identity)

    async def heartbeat(self, job: CrawlerJobInput) -> bool:
        async with self._transaction() as session:
            row = await session.scalar(
                select(CrawlerJob)
                .where(
                    CrawlerJob.id == job.job_id,
                    CrawlerJob.tenant_id == job.tenant_id,
                    CrawlerJob.company_id == job.company_id,
                    CrawlerJob.status == CrawlerJobStatus.RUNNING,
                    CrawlerJob.lease_token == job.lease_token,
                    CrawlerJob.cancel_requested_at.is_(None),
                )
                .with_for_update()
            )
            now = datetime.now(UTC)
            if row is None or row.lease_expires_at is None or row.lease_expires_at <= now:
                return False
            row.heartbeat_at = now
            row.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
            attempt = await self._attempt(session, row)
            if attempt is not None:
                attempt.last_renewed_at = now
                attempt.renewal_count += 1
                emit(
                    "lease_renewal",
                    tenant_id=row.tenant_id,
                    job_id=row.id,
                    attempt_id=attempt.id,
                    worker_id=attempt.worker_id,
                    outcome="renewed",
                )
            return True

    async def cancellation_requested(self, job_id: UUID, tenant_id: UUID) -> bool:
        async with self._read_session() as session:
            return bool(
                await session.scalar(
                    select(CrawlerJob.cancel_requested_at.is_not(None)).where(
                        CrawlerJob.id == job_id,
                        CrawlerJob.tenant_id == tenant_id,
                    )
                )
            )

    async def _owned(self, session: AsyncSession, job: CrawlerJobInput) -> CrawlerJob | None:
        row = await session.scalar(
            select(CrawlerJob)
            .where(
                CrawlerJob.id == job.job_id,
                CrawlerJob.tenant_id == job.tenant_id,
                CrawlerJob.company_id == job.company_id,
                CrawlerJob.status == CrawlerJobStatus.RUNNING,
                CrawlerJob.lease_token == job.lease_token,
            )
            .with_for_update()
        )
        if row is None or row.lease_expires_at is None:
            return None
        if row.lease_expires_at <= datetime.now(UTC):
            return None
        return row

    async def persist_result(self, job: CrawlerJobInput, result: CrawlerJobResult) -> None:
        validated = CrawlerJobResult.model_validate(result)
        async with self._transaction() as session:
            row = await self._owned(session, job)
            if row is None or row.cancel_requested_at is not None:
                raise RuntimeError("crawler lease is no longer owned")
            company = await session.scalar(
                select(Company)
                .where(
                    Company.id == job.company_id,
                    Company.tenant_id == job.tenant_id,
                )
                .with_for_update()
            )
            if company is None:
                raise RuntimeError("crawler company ownership is invalid")
            company.website_status = validated.status
            company.website_checked_at = validated.checked_at
            company.enrichment_status = (
                "failed" if validated.status == "http_error" else "completed"
            )
            if validated.status != "http_error":
                company.last_verified_at = validated.checked_at
            if not company.email and validated.emails:
                company.email = validated.emails[0]
            if not company.phone and validated.phones:
                company.phone = validated.phones[0]
            session.add(
                SourceRecord(
                    tenant_id=job.tenant_id,
                    company_id=job.company_id,
                    source_type="website_verification",
                    source_name="company_website",
                    source_url=job.website_url,
                    captured_data={
                        "status": validated.status,
                        "status_code": validated.status_code,
                        "emails_found": validated.emails,
                        "phones_found": validated.phones,
                        "provenance": "secure_crawler",
                        "crawler_job_id": str(job.job_id),
                    },
                )
            )
            row.result = validated.model_dump(mode="json")
            row.status = CrawlerJobStatus.COMPLETED
            row.finished_at = datetime.now(UTC)
            row.lease_token = None
            row.lease_expires_at = None
            attempt = await self._attempt(session, row)
            if attempt is not None:
                attempt.ended_at = row.finished_at
                attempt.outcome = "completed"
                emit(
                    "completion",
                    tenant_id=row.tenant_id,
                    job_id=row.id,
                    attempt_id=attempt.id,
                    worker_id=attempt.worker_id,
                    outcome="completed",
                )

    async def persist_failure(self, job: CrawlerJobInput, failure_code: str) -> None:
        if failure_code not in {
            "invalid_job",
            "unsafe_website",
            "crawler_error",
            "attempt_timeout",
            "retry_exhausted",
        }:
            failure_code = "crawler_error"
        async with self._transaction() as session:
            row = await self._owned(session, job)
            if row is None or row.cancel_requested_at is not None:
                raise RuntimeError("crawler lease is no longer owned")
            row.failure_code = failure_code
            row.status = CrawlerJobStatus.FAILED
            row.finished_at = datetime.now(UTC)
            row.lease_token = None
            row.lease_expires_at = None
            attempt = await self._attempt(session, row)
            if attempt is not None:
                attempt.ended_at = row.finished_at
                attempt.outcome = "failed"
                attempt.failure_code = failure_code
                emit(
                    "failure",
                    tenant_id=row.tenant_id,
                    job_id=row.id,
                    attempt_id=attempt.id,
                    worker_id=attempt.worker_id,
                    outcome="failed",
                )

    async def persist_cancellation(self, job: CrawlerJobInput) -> None:
        async with self._transaction() as session:
            row = await self._owned(session, job)
            if row is None:
                return
            row.status = CrawlerJobStatus.CANCELLED
            row.finished_at = datetime.now(UTC)
            row.lease_token = None
            row.lease_expires_at = None
            attempt = await self._attempt(session, row)
            if attempt is not None:
                attempt.ended_at = row.finished_at
                attempt.outcome = "cancelled"
                emit(
                    "cancellation",
                    tenant_id=row.tenant_id,
                    job_id=row.id,
                    attempt_id=attempt.id,
                    worker_id=attempt.worker_id,
                    outcome="cancelled",
                )
