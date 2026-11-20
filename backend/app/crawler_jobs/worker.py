"""Worker orchestration; storage and transport are injected at this boundary."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

from app.crawler_jobs.contracts import CrawlerJobInput, CrawlerJobResult, validate_job_input
from app.services.company_enrichment import UnsafeWebsiteError


class CrawlerJobRepository(Protocol):
    """Narrow authorized operations required by a worker; no general DB session API."""

    async def claim_next(self) -> CrawlerJobInput | None: ...

    async def cancellation_requested(self, job_id: UUID, tenant_id: UUID) -> bool: ...

    async def persist_result(
        self, job: CrawlerJobInput, result: CrawlerJobResult
    ) -> None: ...

    async def persist_failure(self, job: CrawlerJobInput, failure_code: str) -> None: ...

    async def persist_cancellation(self, job: CrawlerJobInput) -> None: ...


Crawler = Callable[[str], Awaitable[dict[str, object]]]


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
) -> str:
    """Validate, securely crawl, persist via the narrow port, and return terminal state."""
    try:
        job = validate_job_input(raw_job)
    except (ValueError, TypeError):
        await repository.persist_failure(raw_job, "invalid_job")
        return "failed"

    if await repository.cancellation_requested(job.job_id, job.tenant_id):
        await repository.persist_cancellation(job)
        return "cancelled"
    try:
        # The default injected callable is verify_and_extract; its URL validation,
        # DNSGuard/PinnedResolver, TLS, robots, admission, redirect and byte bounds remain intact.
        raw_result = await crawler(job.website_url)
        result = CrawlerJobResult.model_validate(raw_result)
        if await repository.cancellation_requested(job.job_id, job.tenant_id):
            await repository.persist_cancellation(job)
            return "cancelled"
        await repository.persist_result(job, result)
        return "completed"
    except UnsafeWebsiteError:
        await repository.persist_failure(job, "unsafe_website")
    except asyncio.CancelledError:
        await repository.persist_cancellation(job)
        raise
    except Exception:
        # Do not store exception text, which may contain URLs, response data, or secrets.
        await repository.persist_failure(job, "crawler_error")
    return "failed"
