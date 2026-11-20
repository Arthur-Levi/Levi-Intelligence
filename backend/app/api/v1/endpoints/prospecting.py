import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import TenantContext, require_permission
from app.core.config import get_settings
from app.crawler_jobs.contracts import build_crawler_job
from app.crawler_jobs.events import emit
from app.db import get_db_session
from app.models.prospecting import (
    Company,
    CompanyStatus,
    CrawlerJob,
    CrawlerJobAttempt,
    CrawlerJobStatus,
    DecisionMaker,
    ICPProfile,
    ProspectingRun,
    ProspectingRunStatus,
    SourceRecord,
)
from app.schemas.prospecting import (
    CompanyCreate,
    CompanyResponse,
    CompanyStatusUpdate,
    CrawlerJobResponse,
    DecisionMakerCreate,
    DecisionMakerResponse,
    ICPProfileCreate,
    ICPProfileResponse,
    ProspectingRunCreate,
    ProspectingRunResponse,
    SourceRecordResponse,
)
from app.services.audit import record_audit_event
from app.services.company_enrichment import UnsafeWebsiteError, verify_and_extract
from app.services.company_normalization import normalize_company_name, normalize_website
from app.services.decision_maker_intelligence import classify_job_title, normalize_person_name
from app.services.qualification import qualify_against_icp, qualify_company

router = APIRouter(tags=["prospecting"])


@router.post(
    "/icp-profiles", response_model=ICPProfileResponse, status_code=status.HTTP_201_CREATED
)
async def create_icp_profile(
    payload: ICPProfileCreate,
    request: Request,
    context: TenantContext = Depends(require_permission("icp:write")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> ICPProfileResponse:
    profile = ICPProfile(tenant_id=context.tenant.id, **payload.model_dump())
    session.add(profile)
    try:
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=context.tenant.id,
            actor_user_id=context.user.id,
            action="icp.created",
            resource_type="icp_profile",
            resource_id=str(profile.id),
            ip_address=request.client.host if request.client else None,
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="An ICP with this name already exists"
        ) from None
    await session.refresh(profile)
    return ICPProfileResponse.model_validate(profile)


@router.get("/icp-profiles", response_model=list[ICPProfileResponse])
async def list_icp_profiles(
    context: TenantContext = Depends(require_permission("icp:read")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> list[ICPProfileResponse]:
    rows = await session.scalars(
        select(ICPProfile)
        .where(ICPProfile.tenant_id == context.tenant.id)
        .order_by(ICPProfile.created_at.desc())
    )
    return [ICPProfileResponse.model_validate(row) for row in rows.all()]


@router.post(
    "/prospecting-runs", response_model=ProspectingRunResponse, status_code=status.HTTP_202_ACCEPTED
)
async def create_prospecting_run(
    payload: ProspectingRunCreate,
    request: Request,
    idempotency_key: str = Header(min_length=8, max_length=128, alias="Idempotency-Key"),
    context: TenantContext = Depends(require_permission("prospecting:write")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> ProspectingRunResponse:
    profile = await session.scalar(
        select(ICPProfile).where(
            ICPProfile.id == payload.icp_profile_id,
            ICPProfile.tenant_id == context.tenant.id,
            ICPProfile.is_active.is_(True),
        )
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="ICP profile not found")
    existing = await session.scalar(
        select(ProspectingRun).where(
            ProspectingRun.tenant_id == context.tenant.id,
            ProspectingRun.idempotency_key == idempotency_key,
        )
    )
    fingerprint = sha256(
        json.dumps(payload.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409, detail="Idempotency-Key was already used with a different request"
            )
        return ProspectingRunResponse.model_validate(existing)
    run = ProspectingRun(
        tenant_id=context.tenant.id,
        created_by_user_id=context.user.id,
        icp_profile_id=profile.id,
        provider=payload.provider,
        query=payload.query,
        filters=payload.filters,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        max_attempts=get_settings().discovery_max_attempts,
    )
    session.add(run)
    await session.flush()
    await record_audit_event(
        session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="prospecting.run_queued",
        resource_type="prospecting_run",
        resource_id=str(run.id),
        details={"provider": run.provider, "icp_profile_id": str(profile.id)},
        ip_address=request.client.host if request.client else None,
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(ProspectingRun).where(
                ProspectingRun.tenant_id == context.tenant.id,
                ProspectingRun.idempotency_key == idempotency_key,
            )
        )
        if existing is None or existing.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="Idempotency-Key conflict") from None
        return ProspectingRunResponse.model_validate(existing)
    await session.refresh(run)
    return ProspectingRunResponse.model_validate(run)


@router.get("/prospecting-runs", response_model=list[ProspectingRunResponse])
async def list_prospecting_runs(
    limit: int = Query(default=50, ge=1, le=100),
    context: TenantContext = Depends(require_permission("prospecting:read")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> list[ProspectingRunResponse]:
    rows = await session.scalars(
        select(ProspectingRun)
        .where(ProspectingRun.tenant_id == context.tenant.id)
        .order_by(ProspectingRun.created_at.desc())
        .limit(limit)
    )
    return [ProspectingRunResponse.model_validate(row) for row in rows.all()]


@router.get("/prospecting-runs/{run_id}", response_model=ProspectingRunResponse)
async def get_prospecting_run(
    run_id: UUID,
    context: TenantContext = Depends(require_permission("prospecting:read")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> ProspectingRunResponse:
    run = await session.scalar(
        select(ProspectingRun).where(
            ProspectingRun.id == run_id, ProspectingRun.tenant_id == context.tenant.id
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Prospecting run not found")
    return ProspectingRunResponse.model_validate(run)


@router.post("/prospecting-runs/{run_id}/cancel", response_model=ProspectingRunResponse)
async def cancel_prospecting_run(
    run_id: UUID,
    request: Request,
    context: TenantContext = Depends(require_permission("prospecting:write")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> ProspectingRunResponse:
    run = await session.scalar(
        select(ProspectingRun)
        .where(ProspectingRun.id == run_id, ProspectingRun.tenant_id == context.tenant.id)
        .with_for_update()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Prospecting run not found")
    if run.status in {
        ProspectingRunStatus.COMPLETED,
        ProspectingRunStatus.FAILED,
        ProspectingRunStatus.CANCELLED,
    }:
        raise HTTPException(status_code=409, detail="A terminal run cannot be cancelled")
    run.cancel_requested_at = datetime.now(UTC)
    if run.status == ProspectingRunStatus.QUEUED:
        run.status = ProspectingRunStatus.CANCELLED
        run.finished_at = datetime.now(UTC)
    await record_audit_event(
        session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="prospecting.run_cancel_requested",
        resource_type="prospecting_run",
        resource_id=str(run.id),
        ip_address=request.client.host if request.client else None,
    )
    await session.commit()
    await session.refresh(run)
    return ProspectingRunResponse.model_validate(run)


@router.post("/companies", response_model=CompanyResponse, status_code=status.HTTP_201_CREATED)
async def create_company(
    payload: CompanyCreate,
    request: Request,
    context: TenantContext = Depends(require_permission("companies:write")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> CompanyResponse:
    normalized_url, domain = normalize_website(payload.website_url)
    company = Company(
        tenant_id=context.tenant.id,
        name=payload.name.strip(),
        website_url=normalized_url,
        normalized_domain=domain,
        normalized_name=normalize_company_name(payload.name),
        phone=payload.phone,
        email=payload.email.lower() if payload.email else None,
        address=payload.address,
        industry=payload.industry,
        employee_count=payload.employee_count,
        description=payload.description,
        first_source=payload.source_name,
        source_url=payload.source_url,
    )
    company.qualification_score, company.qualification_reasons = qualify_company(company)
    session.add(company)
    try:
        await session.flush()
        source = SourceRecord(
            tenant_id=context.tenant.id,
            company_id=company.id,
            source_type="manual",
            source_name=payload.source_name,
            source_url=payload.source_url,
            captured_data={"company_name": company.name, "website_url": company.website_url},
        )
        session.add(source)
        await record_audit_event(
            session,
            tenant_id=context.tenant.id,
            actor_user_id=context.user.id,
            action="company.created",
            resource_type="company",
            resource_id=str(company.id),
            details={"source": payload.source_name},
            ip_address=request.client.host if request.client else None,
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="A company with this website already exists in this organization",
        ) from None
    await session.refresh(company)
    return CompanyResponse.model_validate(company)


@router.get("/companies", response_model=list[CompanyResponse])
async def list_companies(
    search: str | None = Query(default=None, min_length=2, max_length=120),
    company_status: CompanyStatus | None = Query(default=None, alias="status"),  # noqa: B008
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: TenantContext = Depends(require_permission("companies:read")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> list[CompanyResponse]:
    statement = select(Company).where(Company.tenant_id == context.tenant.id)
    if search:
        statement = statement.where(Company.name.ilike(f"%{search}%"))
    if company_status:
        statement = statement.where(Company.status == company_status)
    rows = await session.scalars(
        statement.order_by(Company.created_at.desc()).offset(offset).limit(limit)
    )
    return [CompanyResponse.model_validate(row) for row in rows.all()]


@router.post(
    "/companies/{company_id}/enrich/jobs",
    response_model=CrawlerJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_company_enrichment_job(
    company_id: UUID,
    request: Request,
    idempotency_key: str = Header(min_length=8, max_length=128, alias="Idempotency-Key"),
    context: TenantContext = Depends(require_permission("companies:write")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> CrawlerJobResponse:
    company = await session.scalar(
        select(Company).where(Company.id == company_id, Company.tenant_id == context.tenant.id)
    )
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    if not company.website_url:
        raise HTTPException(status_code=422, detail="Company has no website to verify")
    job = build_crawler_job(
        tenant_id=context.tenant.id,
        company_id=company.id,
        actor_user_id=context.user.id,
        website_url=company.website_url,
        idempotency_key=idempotency_key,
    )
    existing = await session.scalar(
        select(CrawlerJob).where(
            CrawlerJob.tenant_id == context.tenant.id,
            CrawlerJob.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != job.request_fingerprint:
            emit(
                "idempotency_conflict",
                tenant_id=context.tenant.id,
                job_id=existing.id,
                outcome="conflict",
            )
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key was already used with a different request",
            )
        return CrawlerJobResponse.model_validate(existing)

    # Serialize admission decisions across API processes. Recheck idempotency first so
    # a duplicate request remains idempotent even while the queue is at capacity.
    await session.execute(text("SELECT pg_advisory_xact_lock(681204117)"))
    existing = await session.scalar(
        select(CrawlerJob).where(
            CrawlerJob.tenant_id == context.tenant.id,
            CrawlerJob.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != job.request_fingerprint:
            emit(
                "idempotency_conflict",
                tenant_id=context.tenant.id,
                job_id=existing.id,
                outcome="conflict",
            )
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key was already used with a different request",
            )
        return CrawlerJobResponse.model_validate(existing)
    queued_global = await session.scalar(
        select(func.count())
        .select_from(CrawlerJob)
        .where(
            CrawlerJob.status == CrawlerJobStatus.QUEUED,
        )
    )
    queued_tenant = await session.scalar(
        select(func.count())
        .select_from(CrawlerJob)
        .where(
            CrawlerJob.tenant_id == context.tenant.id,
            CrawlerJob.status == CrawlerJobStatus.QUEUED,
        )
    )
    if (queued_global or 0) >= 10000:
        emit("capacity_rejection", tenant_id=context.tenant.id, outcome="rejected")
        raise HTTPException(status_code=429, detail={"code": "crawler_queue_full"})
    if (queued_tenant or 0) >= 100:
        emit("capacity_rejection", tenant_id=context.tenant.id, outcome="rejected")
        raise HTTPException(status_code=429, detail={"code": "crawler_tenant_queue_full"})

    try:
        session.add(job)
        company.enrichment_status = "queued"
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=context.tenant.id,
            actor_user_id=context.user.id,
            action="company.enrichment_queued",
            resource_type="crawler_job",
            resource_id=str(job.id),
            details={"company_id": str(company.id)},
            ip_address=request.client.host if request.client else None,
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(CrawlerJob).where(
                CrawlerJob.tenant_id == context.tenant.id,
                CrawlerJob.idempotency_key == idempotency_key,
            )
        )
        if existing is None or existing.request_fingerprint != job.request_fingerprint:
            raise HTTPException(status_code=409, detail="Idempotency-Key conflict") from None
        return CrawlerJobResponse.model_validate(existing)
    await session.refresh(job)
    emit("admission", tenant_id=context.tenant.id, job_id=job.id, outcome="accepted")
    return CrawlerJobResponse.model_validate(job)


@router.post("/companies/{company_id}/enrich", response_model=CompanyResponse)
async def enrich_company(
    company_id: UUID,
    request: Request,
    context: TenantContext = Depends(require_permission("companies:write")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> CompanyResponse:
    """Compatibility route; clients should migrate to the asynchronous /enrich/jobs route."""
    company = await session.scalar(
        select(Company).where(Company.id == company_id, Company.tenant_id == context.tenant.id)
    )
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    if not company.website_url:
        raise HTTPException(status_code=422, detail="Company has no website to verify")
    try:
        result = await verify_and_extract(company.website_url)
    except UnsafeWebsiteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        company.website_status = "verification_error"
        company.enrichment_status = "failed"
        await session.commit()
        raise HTTPException(status_code=502, detail="Website verification failed") from None
    company.website_status = result["status"]
    company.website_checked_at = result["checked_at"]
    if result["status"] != "http_error":
        company.last_verified_at = result["checked_at"]
    if not company.email and result.get("emails"):
        company.email = result["emails"][0]
    if not company.phone and result.get("phones"):
        company.phone = result["phones"][0]
    company.enrichment_status = "failed" if result["status"] == "http_error" else "completed"
    company.qualification_score, company.qualification_reasons = qualify_company(company)
    session.add(
        SourceRecord(
            tenant_id=context.tenant.id,
            company_id=company.id,
            source_type="website_verification",
            source_name="company_website",
            source_url=company.website_url,
            captured_data={
                "status": result["status"],
                "status_code": result.get("status_code"),
                "emails_found": result.get("emails", []),
                "phones_found": result.get("phones", []),
            },
        )
    )
    await record_audit_event(
        session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="company.enriched",
        resource_type="company",
        resource_id=str(company.id),
        details={"website_status": company.website_status},
        ip_address=request.client.host if request.client else None,
    )
    await session.commit()
    await session.refresh(company)
    return CompanyResponse.model_validate(company)


@router.get("/crawler-jobs/{job_id}", response_model=CrawlerJobResponse)
async def get_crawler_job(
    job_id: UUID,
    context: TenantContext = Depends(require_permission("companies:read")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> CrawlerJobResponse:
    job = await session.scalar(
        select(CrawlerJob).where(CrawlerJob.id == job_id, CrawlerJob.tenant_id == context.tenant.id)
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Crawler job not found")
    return CrawlerJobResponse.model_validate(job)


@router.post("/crawler-jobs/{job_id}/cancel", response_model=CrawlerJobResponse)
async def cancel_crawler_job(
    job_id: UUID,
    request: Request,
    context: TenantContext = Depends(require_permission("companies:write")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> CrawlerJobResponse:
    job = await session.scalar(
        select(CrawlerJob)
        .where(CrawlerJob.id == job_id, CrawlerJob.tenant_id == context.tenant.id)
        .with_for_update()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Crawler job not found")
    if job.status in {
        CrawlerJobStatus.COMPLETED,
        CrawlerJobStatus.FAILED,
        CrawlerJobStatus.CANCELLED,
    }:
        raise HTTPException(status_code=409, detail="A terminal crawler job cannot be cancelled")
    job.cancel_requested_at = datetime.now(UTC)
    job.status = CrawlerJobStatus.CANCELLED
    job.finished_at = datetime.now(UTC)
    job.lease_token = None
    job.lease_expires_at = None
    if job.attempts:
        attempt = await session.scalar(
            select(CrawlerJobAttempt).where(
                CrawlerJobAttempt.tenant_id == job.tenant_id,
                CrawlerJobAttempt.job_id == job.id,
                CrawlerJobAttempt.generation == job.attempts,
            )
        )
        if attempt is not None and attempt.ended_at is None:
            attempt.ended_at = job.finished_at
            attempt.outcome = "cancelled"
    await record_audit_event(
        session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="crawler_job.cancel_requested",
        resource_type="crawler_job",
        resource_id=str(job.id),
        ip_address=request.client.host if request.client else None,
    )
    await session.commit()
    await session.refresh(job)
    emit("cancellation", tenant_id=context.tenant.id, job_id=job.id, outcome="cancelled")
    return CrawlerJobResponse.model_validate(job)


@router.post("/companies/{company_id}/qualify/{icp_id}", response_model=CompanyResponse)
async def qualify_company_for_icp(
    company_id: UUID,
    icp_id: UUID,
    request: Request,
    context: TenantContext = Depends(require_permission("companies:write")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> CompanyResponse:
    company = await session.scalar(
        select(Company).where(Company.id == company_id, Company.tenant_id == context.tenant.id)
    )
    icp = await session.scalar(
        select(ICPProfile).where(
            ICPProfile.id == icp_id,
            ICPProfile.tenant_id == context.tenant.id,
            ICPProfile.is_active.is_(True),
        )
    )
    if company is None or icp is None:
        raise HTTPException(status_code=404, detail="Company or ICP profile not found")
    result = qualify_against_icp(company, icp)
    company.opportunity_score = result["opportunity_score"]
    company.qualification_confidence = result["confidence"]
    company.qualification_breakdown = result
    company.qualification_score = result["fit_score"]
    company.qualification_reasons = [item["reason"] for item in result["checks"]]
    await record_audit_event(
        session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="company.qualified",
        resource_type="company",
        resource_id=str(company.id),
        details={
            "icp_id": str(icp.id),
            "fit_score": result["fit_score"],
            "opportunity_score": result["opportunity_score"],
        },
        ip_address=request.client.host if request.client else None,
    )
    await session.commit()
    await session.refresh(company)
    return CompanyResponse.model_validate(company)


@router.patch("/companies/{company_id}/status", response_model=CompanyResponse)
async def update_company_status(
    company_id: UUID,
    payload: CompanyStatusUpdate,
    request: Request,
    context: TenantContext = Depends(require_permission("companies:write")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> CompanyResponse:
    company = await session.scalar(
        select(Company).where(Company.id == company_id, Company.tenant_id == context.tenant.id)
    )
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    previous = company.status
    company.status = payload.status
    await record_audit_event(
        session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="company.status_updated",
        resource_type="company",
        resource_id=str(company.id),
        details={"from": previous.value, "to": payload.status.value},
        ip_address=request.client.host if request.client else None,
    )
    await session.commit()
    await session.refresh(company)
    return CompanyResponse.model_validate(company)


@router.get("/companies/{company_id}/sources", response_model=list[SourceRecordResponse])
async def list_company_sources(
    company_id: UUID,
    context: TenantContext = Depends(require_permission("companies:read")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> list[SourceRecordResponse]:
    company_exists = await session.scalar(
        select(Company.id).where(Company.id == company_id, Company.tenant_id == context.tenant.id)
    )
    if company_exists is None:
        raise HTTPException(status_code=404, detail="Company not found")
    rows = await session.scalars(
        select(SourceRecord)
        .where(SourceRecord.company_id == company_id, SourceRecord.tenant_id == context.tenant.id)
        .order_by(SourceRecord.captured_at.desc())
    )
    return [SourceRecordResponse.model_validate(row) for row in rows.all()]


@router.post(
    "/companies/{company_id}/decision-makers",
    response_model=DecisionMakerResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_decision_maker(
    company_id: UUID,
    payload: DecisionMakerCreate,
    request: Request,
    context: TenantContext = Depends(require_permission("companies:write")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> DecisionMakerResponse:
    company = await session.scalar(
        select(Company).where(Company.id == company_id, Company.tenant_id == context.tenant.id)
    )
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    person = DecisionMaker(
        tenant_id=context.tenant.id,
        company_id=company.id,
        normalized_name=normalize_person_name(payload.full_name),
        **{
            **payload.model_dump(),
            "role_category": payload.role_category
            if payload.role_category.value != "other"
            else classify_job_title(payload.job_title),
        },
    )
    session.add(person)
    await session.flush()
    await record_audit_event(
        session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="decision_maker.created",
        resource_type="decision_maker",
        resource_id=str(person.id),
        details={"company_id": str(company.id)},
        ip_address=request.client.host if request.client else None,
    )
    await session.commit()
    await session.refresh(person)
    return DecisionMakerResponse.model_validate(person)


@router.get("/companies/{company_id}/decision-makers", response_model=list[DecisionMakerResponse])
async def list_decision_makers(
    company_id: UUID,
    context: TenantContext = Depends(require_permission("companies:read")),  # noqa: B008
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> list[DecisionMakerResponse]:
    company_exists = await session.scalar(
        select(Company.id).where(Company.id == company_id, Company.tenant_id == context.tenant.id)
    )
    if company_exists is None:
        raise HTTPException(status_code=404, detail="Company not found")
    rows = await session.scalars(
        select(DecisionMaker)
        .where(DecisionMaker.tenant_id == context.tenant.id, DecisionMaker.company_id == company_id)
        .order_by(DecisionMaker.created_at.desc())
    )
    return [DecisionMakerResponse.model_validate(row) for row in rows.all()]
