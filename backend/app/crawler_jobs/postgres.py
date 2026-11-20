"""Narrow PostgreSQL adapter for the crawler job lifecycle."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crawler_jobs.contracts import CrawlerJobInput, CrawlerJobResult
from app.models.prospecting import Company, CrawlerJob, CrawlerJobStatus, SourceRecord

MAX_ATTEMPTS = 5
LEASE_SECONDS = 90


class PostgresCrawlerJobRepository:
    """Exposes only crawler lifecycle operations; each write has its own transaction."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    @staticmethod
    def _payload(row: CrawlerJob, token: UUID) -> CrawlerJobInput:
        return CrawlerJobInput(
            job_id=row.id,
            tenant_id=row.tenant_id,
            company_id=row.company_id,
            website_url=row.website_url,
            idempotency_key=row.idempotency_key,
            lease_token=token,
        )

    async def claim_next(self, tenant_id: UUID | None = None) -> CrawlerJobInput | None:
        now = datetime.now(UTC)
        async with self._factory() as session, session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(681204117)"))
            # Expired attempts are retried with bounded exponential backoff or dead-lettered.
            expired = (CrawlerJob.status == CrawlerJobStatus.RUNNING) & (
                CrawlerJob.lease_expires_at <= now
            )
            rows = await session.scalars(
                select(CrawlerJob).where(expired).with_for_update(skip_locked=True).limit(32)
            )
            for row in rows:
                row.lease_token = None
                row.lease_expires_at = None
                if row.cancel_requested_at is not None:
                    row.status = CrawlerJobStatus.CANCELLED
                    row.finished_at = now
                elif row.attempts >= MAX_ATTEMPTS:
                    row.status = CrawlerJobStatus.FAILED
                    row.failure_code = "retry_exhausted"
                    row.finished_at = now
                else:
                    row.status = CrawlerJobStatus.QUEUED
                    row.next_attempt_at = now + timedelta(seconds=min(60, 2**row.attempts))

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
            return self._payload(selected, token)

    async def heartbeat(self, job: CrawlerJobInput) -> bool:
        now = datetime.now(UTC)
        async with self._factory() as session, session.begin():
            row = await session.scalar(
                select(CrawlerJob)
                .where(
                    CrawlerJob.id == job.job_id,
                    CrawlerJob.tenant_id == job.tenant_id,
                    CrawlerJob.company_id == job.company_id,
                    CrawlerJob.status == CrawlerJobStatus.RUNNING,
                    CrawlerJob.lease_token == job.lease_token,
                    CrawlerJob.lease_expires_at > now,
                    CrawlerJob.cancel_requested_at.is_(None),
                )
                .with_for_update()
            )
            if row is None:
                return False
            row.heartbeat_at = now
            row.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
            return True

    async def cancellation_requested(self, job_id: UUID, tenant_id: UUID) -> bool:
        async with self._factory() as session:
            return bool(
                await session.scalar(
                    select(CrawlerJob.cancel_requested_at.is_not(None)).where(
                        CrawlerJob.id == job_id,
                        CrawlerJob.tenant_id == tenant_id,
                    )
                )
            )

    async def _owned(self, session: AsyncSession, job: CrawlerJobInput) -> CrawlerJob | None:
        now = datetime.now(UTC)
        return await session.scalar(
            select(CrawlerJob)
            .where(
                CrawlerJob.id == job.job_id,
                CrawlerJob.tenant_id == job.tenant_id,
                CrawlerJob.company_id == job.company_id,
                CrawlerJob.status == CrawlerJobStatus.RUNNING,
                CrawlerJob.lease_token == job.lease_token,
                CrawlerJob.lease_expires_at > now,
            )
            .with_for_update()
        )

    async def persist_result(self, job: CrawlerJobInput, result: CrawlerJobResult) -> None:
        validated = CrawlerJobResult.model_validate(result)
        async with self._factory() as session, session.begin():
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

    async def persist_failure(self, job: CrawlerJobInput, failure_code: str) -> None:
        if failure_code not in {
            "invalid_job",
            "unsafe_website",
            "crawler_error",
            "retry_exhausted",
        }:
            failure_code = "crawler_error"
        async with self._factory() as session, session.begin():
            row = await self._owned(session, job)
            if row is None or row.cancel_requested_at is not None:
                raise RuntimeError("crawler lease is no longer owned")
            row.failure_code = failure_code
            row.status = CrawlerJobStatus.FAILED
            row.finished_at = datetime.now(UTC)
            row.lease_token = None
            row.lease_expires_at = None

    async def persist_cancellation(self, job: CrawlerJobInput) -> None:
        async with self._factory() as session, session.begin():
            row = await self._owned(session, job)
            if row is None:
                return
            row.status = CrawlerJobStatus.CANCELLED
            row.finished_at = datetime.now(UTC)
            row.lease_token = None
            row.lease_expires_at = None
