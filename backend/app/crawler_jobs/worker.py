"""Worker orchestration; storage and transport are injected at this boundary."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

from app.crawler_jobs.contracts import CrawlerJobInput, CrawlerJobResult, validate_job_input
from app.crawler_jobs.postgres import CrawlerDatabaseError
from app.crawler_jobs.process_executor import ChildTerminationError
from app.crawler_jobs.timing import CrawlerTiming
from app.services.company_enrichment import UnsafeWebsiteError


class CrawlerJobRepository(Protocol):
    """Narrow authorized operations required by a worker; no general DB session API."""

    async def claim_next(self) -> CrawlerJobInput | None: ...

    async def cancellation_requested(self, job_id: UUID, tenant_id: UUID) -> bool: ...

    async def persist_result(self, job: CrawlerJobInput, result: CrawlerJobResult) -> None: ...

    async def persist_failure(self, job: CrawlerJobInput, failure_code: str) -> None: ...

    async def persist_cancellation(self, job: CrawlerJobInput) -> None: ...


Crawler = Callable[[str], Awaitable[dict[str, object]]]


class NonCooperativeCrawlerError(RuntimeError):
    """A cancelled child outlived its bounded cancellation window."""


async def execute_next_crawler_job(
    *, repository: CrawlerJobRepository, crawler: Crawler | None = None
) -> bool:
    """Claim one job through the narrow port and invoke the preserved secure service."""
    from app.services.company_enrichment import verify_and_extract

    job = await repository.claim_next()
    if job is None:
        return False
    await execute_crawler_job(job, repository=repository, crawler=crawler or verify_and_extract)
    return True


async def execute_crawler_job(
    raw_job: CrawlerJobInput,
    *,
    repository: CrawlerJobRepository,
    crawler: Crawler,
    timing: CrawlerTiming | None = None,
    acquired_at: float | None = None,
) -> str:
    """Validate, securely crawl, persist via the narrow port, and return terminal state."""
    timing = timing or CrawlerTiming()
    deadline = (time.monotonic() if acquired_at is None else acquired_at) + timing.attempt_seconds
    try:
        job = validate_job_input(raw_job)
    except (ValueError, TypeError):
        await repository.persist_failure(raw_job, "invalid_job")
        return "failed"

    if await repository.cancellation_requested(job.job_id, job.tenant_id):
        await repository.persist_cancellation(job)
        return "cancelled"

    async def crawl_with_lease(url: str) -> dict[str, object]:
        crawl_task: asyncio.Future[dict[str, object]] = asyncio.ensure_future(crawler(url))
        heartbeat = getattr(repository, "heartbeat", None)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                done, _ = await asyncio.wait({crawl_task}, timeout=min(5, remaining))
                if done:
                    return await crawl_task
                if time.monotonic() >= deadline:
                    raise TimeoutError
                if await repository.cancellation_requested(job.job_id, job.tenant_id):
                    raise asyncio.CancelledError
                if heartbeat is not None and not await heartbeat(job):
                    raise RuntimeError("crawler execution lease lost")
        except BaseException as exc:
            if not crawl_task.done():
                crawl_task.cancel()
                done, _ = await asyncio.wait({crawl_task}, timeout=timing.cancellation_seconds)
                if not done:
                    raise NonCooperativeCrawlerError(
                        "crawler child did not stop after cancellation"
                    ) from exc
                await asyncio.gather(crawl_task, return_exceptions=True)
            raise

    try:
        if time.monotonic() >= deadline:
            raise TimeoutError
        # The default injected callable is verify_and_extract; its URL validation,
        # DNSGuard/PinnedResolver, TLS, robots, admission, redirect and byte bounds remain intact.
        raw_result = await crawl_with_lease(job.website_url)
        if time.monotonic() >= deadline:
            raise TimeoutError
        result = CrawlerJobResult.model_validate(raw_result)
    except TimeoutError:
        await repository.persist_failure(job, "attempt_timeout")
        return "failed"
    except NonCooperativeCrawlerError:
        raise
    except ChildTerminationError:
        # A child that has not been reaped cannot be finalized safely.
        raise
    except CrawlerDatabaseError:
        # Heartbeat and cancellation-read uncertainty is not a crawl failure.
        # Preserve the lease for fenced recovery instead of manufacturing a result.
        raise
    except UnsafeWebsiteError:
        await repository.persist_failure(job, "unsafe_website")
        return "failed"
    except asyncio.CancelledError:
        # A worker shutdown is not a user cancellation. Leave the lease and
        # attempt ledger intact so PostgreSQL recovery can safely reclaim it.
        if await repository.cancellation_requested(job.job_id, job.tenant_id):
            await repository.persist_cancellation(job)
        raise
    except Exception:
        # Do not store exception text, which may contain URLs, response data, or secrets.
        await repository.persist_failure(job, "crawler_error")
        return "failed"
    if await repository.cancellation_requested(job.job_id, job.tenant_id):
        await repository.persist_cancellation(job)
        return "cancelled"
    if time.monotonic() >= deadline:
        await repository.persist_failure(job, "attempt_timeout")
        return "failed"
    # Persistence failures propagate; they are not misclassified as crawl failures.
    await repository.persist_result(job, result)
    return "completed"
