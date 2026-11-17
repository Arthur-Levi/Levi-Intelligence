import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db import get_session_factory
from app.integrations.apify import ApifyClient
from app.integrations.prospecting import DiscoveryRequest, ManualDiscoveryProvider
from app.models.prospecting import Company, ICPProfile, ProspectingRun, ProspectingRunStatus, SourceRecord
from app.services.company_normalization import normalize_company_name, normalize_website
from app.services.qualification import qualify_company

logger = logging.getLogger("levi.discovery")


async def _claim_one(factory: async_sessionmaker[AsyncSession]) -> UUID | None:
    async with factory() as session:
        async with session.begin():
            now = datetime.now(UTC)
            run = await session.scalar(
                select(ProspectingRun)
                .where(
                    ProspectingRun.status == ProspectingRunStatus.QUEUED,
                    (ProspectingRun.next_retry_at.is_(None) | (ProspectingRun.next_retry_at <= now)),
                )
                .order_by(ProspectingRun.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if run is None:
                return None
            run.status = ProspectingRunStatus.RUNNING
            run.attempts += 1
            run.started_at = now
            run.last_error = None
            return run.id


async def _is_cancelled(session: AsyncSession, run_id: UUID) -> bool:
    run = await session.get(ProspectingRun, run_id)
    return run is None or run.cancel_requested_at is not None or run.status == ProspectingRunStatus.CANCELLED


async def _process_run(factory: async_sessionmaker[AsyncSession], run_id: UUID) -> None:
    async with factory() as session:
        run = await session.get(ProspectingRun, run_id)
        if run is None:
            return
        icp = await session.scalar(select(ICPProfile).where(
            ICPProfile.id == run.icp_profile_id, ICPProfile.tenant_id == run.tenant_id
        ))
        if icp is None:
            raise RuntimeError("ICP profile no longer exists for this tenant")
        tenant_id = run.tenant_id
        request = DiscoveryRequest(query=run.query, filters=run.filters, icp={
            "industry": icp.industry, "geography": icp.geography,
            "company_size": icp.company_size, "criteria": icp.criteria,
            "exclusions": icp.exclusions,
        })
        provider_name = run.provider

    settings = get_settings()
    if provider_name == "manual":
        discovered = await ManualDiscoveryProvider().discover(request)
    elif provider_name == "apify":
        if not settings.apify_api_token or not settings.apify_actor_id:
            raise RuntimeError("Apify integration requires APIFY_API_TOKEN and APIFY_ACTOR_ID")
        discovered = await ApifyClient(settings.apify_api_token, settings.apify_actor_id).discover(request)
    else:
        raise RuntimeError(f"Unsupported discovery provider: {provider_name}")

    async with factory() as session:
        async with session.begin():
            run = await session.scalar(select(ProspectingRun).where(ProspectingRun.id == run_id).with_for_update())
            if run is None:
                return
            if run.cancel_requested_at is not None:
                run.status = ProspectingRunStatus.CANCELLED
                run.finished_at = datetime.now(UTC)
                return
            imported = 0
            for item in discovered:
                website, domain = normalize_website(item.website_url)
                company = None
                if domain:
                    company = await session.scalar(select(Company).where(
                        Company.tenant_id == tenant_id, Company.normalized_domain == domain
                    ).with_for_update())
                if company is None:
                    company = Company(
                        tenant_id=tenant_id, name=item.name,
                        normalized_name=normalize_company_name(item.name),
                        website_url=website, normalized_domain=domain,
                        phone=item.phone, email=item.email.lower() if item.email else None,
                        address=item.address or {}, industry=item.industry,
                        first_source=provider_name, source_url=item.source_url,
                        enrichment_status="pending",
                    )
                    session.add(company)
                    await session.flush()
                    imported += 1
                if item.external_id:
                    exists = await session.scalar(select(SourceRecord.id).where(
                        SourceRecord.tenant_id == tenant_id,
                        SourceRecord.company_id == company.id,
                        SourceRecord.external_id == item.external_id,
                        SourceRecord.source_name == provider_name,
                    ))
                else:
                    exists = None
                if not exists:
                    session.add(SourceRecord(
                        tenant_id=tenant_id, company_id=company.id, prospecting_run_id=run.id,
                        source_type="discovery", source_name=provider_name,
                        source_url=item.source_url, external_id=item.external_id,
                        captured_data=item.raw or {"name": item.name, "website_url": item.website_url},
                    ))
                company.qualification_score, company.qualification_reasons = qualify_company(company)
            run.companies_found = len(discovered)
            run.companies_imported = imported
            run.status = ProspectingRunStatus.COMPLETED
            run.finished_at = datetime.now(UTC)


async def _mark_failure(factory: async_sessionmaker[AsyncSession], run_id: UUID, error: Exception) -> None:
    async with factory() as session:
        async with session.begin():
            run = await session.scalar(select(ProspectingRun).where(ProspectingRun.id == run_id).with_for_update())
            if run is None:
                return
            if run.cancel_requested_at is not None:
                run.status = ProspectingRunStatus.CANCELLED
                run.finished_at = datetime.now(UTC)
                return
            message = f"{type(error).__name__}: {str(error)[:1500]}"
            run.last_error = message
            run.error_message = message
            if run.attempts < run.max_attempts:
                run.status = ProspectingRunStatus.QUEUED
                run.next_retry_at = datetime.now(UTC) + timedelta(seconds=min(300, 2 ** run.attempts * 5))
            else:
                run.status = ProspectingRunStatus.FAILED
                run.finished_at = datetime.now(UTC)
            logger.warning("Discovery run %s failed (attempt %s/%s): %s", run.id, run.attempts, run.max_attempts, message)


async def run_worker_once(factory: async_sessionmaker[AsyncSession] | None = None) -> bool:
    factory = factory or get_session_factory()
    run_id = await _claim_one(factory)
    if run_id is None:
        return False
    try:
        await _process_run(factory, run_id)
    except Exception as exc:  # worker boundary: persist failure and retry policy
        await _mark_failure(factory, run_id, exc)
    return True


async def worker_loop() -> None:
    settings = get_settings()
    factory = get_session_factory()
    logger.info("Discovery worker started")
    while True:
        processed = await run_worker_once(factory)
        if not processed:
            await asyncio.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(worker_loop())
