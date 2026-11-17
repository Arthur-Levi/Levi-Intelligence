from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import TenantContext, require_permission
from app.db import get_db_session
from app.models.prospecting import (
    Company,
    DecisionMaker,
    DecisionMakerChange,
    DecisionMakerEvidence,
)
from app.schemas.decision_makers import (
    DecisionMakerChangeResponse,
    DecisionMakerEvidenceCreate,
    DecisionMakerEvidenceResponse,
    DecisionMakerIntelligenceResponse,
    DecisionMakerUpdate,
)
from app.services.audit import record_audit_event
from app.services.decision_maker_intelligence import (
    authority_for_source,
    calculate_decision_maker_confidence,
    classify_job_title,
    normalize_person_name,
)

router = APIRouter(tags=["decision-maker-intelligence"])


async def _get_person(session: AsyncSession, tenant_id: UUID, person_id: UUID) -> DecisionMaker:
    person = await session.scalar(
        select(DecisionMaker).where(
            DecisionMaker.id == person_id,
            DecisionMaker.tenant_id == tenant_id,
        )
    )
    if person is None:
        raise HTTPException(status_code=404, detail="Decision maker not found")
    return person


@router.get("/companies/{company_id}/decision-makers/intelligence", response_model=list[DecisionMakerIntelligenceResponse])
async def list_decision_maker_intelligence(
    company_id: UUID,
    context: TenantContext = Depends(require_permission("companies:read")),
    session: AsyncSession = Depends(get_db_session),
) -> list[DecisionMakerIntelligenceResponse]:
    company = await session.scalar(select(Company.id).where(
        Company.id == company_id, Company.tenant_id == context.tenant.id
    ))
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    rows = await session.scalars(
        select(DecisionMaker).where(
            DecisionMaker.tenant_id == context.tenant.id,
            DecisionMaker.company_id == company_id,
        ).order_by(DecisionMaker.confidence_score.desc(), DecisionMaker.created_at.desc())
    )
    return [DecisionMakerIntelligenceResponse.model_validate(row) for row in rows.all()]


@router.post("/decision-makers/{person_id}/evidence", response_model=DecisionMakerEvidenceResponse, status_code=status.HTTP_201_CREATED)
async def add_decision_maker_evidence(
    person_id: UUID,
    payload: DecisionMakerEvidenceCreate,
    request: Request,
    context: TenantContext = Depends(require_permission("companies:write")),
    session: AsyncSession = Depends(get_db_session),
) -> DecisionMakerEvidenceResponse:
    person = await _get_person(session, context.tenant.id, person_id)
    authority = authority_for_source(payload.source_type)
    evidence = DecisionMakerEvidence(
        tenant_id=context.tenant.id,
        decision_maker_id=person.id,
        claim_type=payload.claim_type,
        source_type=payload.source_type,
        source_name=payload.source_name.strip(),
        source_url=payload.source_url,
        source_published_at=payload.source_published_at,
        excerpt=payload.excerpt,
        authority_weight=authority,
    )
    session.add(evidence)
    await session.flush()
    before = {
        "status": person.status.value,
        "confidence_score": person.confidence_score,
        "confidence_level": person.confidence_level,
        "job_title": person.job_title,
    }
    rows = await session.scalars(select(DecisionMakerEvidence).where(
        DecisionMakerEvidence.tenant_id == context.tenant.id,
        DecisionMakerEvidence.decision_maker_id == person.id,
    ))
    result = calculate_decision_maker_confidence(rows.all())
    person.confidence_score = int(result["score"])
    person.confidence_level = str(result["level"])
    person.status = result["status"]
    person.last_verified_at = datetime.now(UTC)
    after = {
        "status": person.status.value,
        "confidence_score": person.confidence_score,
        "confidence_level": person.confidence_level,
        "job_title": person.job_title,
        "evidence_id": str(evidence.id),
        "assessment": result,
    }
    session.add(DecisionMakerChange(
        tenant_id=context.tenant.id,
        decision_maker_id=person.id,
        change_type="evidence_added_and_reassessed",
        before=before,
        after=after,
        source_evidence_id=evidence.id,
    ))
    await record_audit_event(
        session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="decision_maker.evidence_added",
        resource_type="decision_maker",
        resource_id=str(person.id),
        details={"claim_type": payload.claim_type, "source_type": payload.source_type, "confidence": person.confidence_score},
        ip_address=request.client.host if request.client else None,
    )
    await session.commit()
    await session.refresh(evidence)
    return DecisionMakerEvidenceResponse.model_validate(evidence)


@router.get("/decision-makers/{person_id}/evidence", response_model=list[DecisionMakerEvidenceResponse])
async def list_decision_maker_evidence(
    person_id: UUID,
    context: TenantContext = Depends(require_permission("companies:read")),
    session: AsyncSession = Depends(get_db_session),
) -> list[DecisionMakerEvidenceResponse]:
    await _get_person(session, context.tenant.id, person_id)
    rows = await session.scalars(select(DecisionMakerEvidence).where(
        DecisionMakerEvidence.tenant_id == context.tenant.id,
        DecisionMakerEvidence.decision_maker_id == person_id,
    ).order_by(DecisionMakerEvidence.observed_at.desc()))
    return [DecisionMakerEvidenceResponse.model_validate(row) for row in rows.all()]


@router.get("/decision-makers/{person_id}/history", response_model=list[DecisionMakerChangeResponse])
async def list_decision_maker_history(
    person_id: UUID,
    context: TenantContext = Depends(require_permission("companies:read")),
    session: AsyncSession = Depends(get_db_session),
) -> list[DecisionMakerChangeResponse]:
    await _get_person(session, context.tenant.id, person_id)
    rows = await session.scalars(select(DecisionMakerChange).where(
        DecisionMakerChange.tenant_id == context.tenant.id,
        DecisionMakerChange.decision_maker_id == person_id,
    ).order_by(DecisionMakerChange.created_at.desc()))
    return [DecisionMakerChangeResponse.model_validate(row) for row in rows.all()]


@router.post("/decision-makers/{person_id}/reassess", response_model=DecisionMakerIntelligenceResponse)
async def reassess_decision_maker(
    person_id: UUID,
    request: Request,
    context: TenantContext = Depends(require_permission("companies:write")),
    session: AsyncSession = Depends(get_db_session),
) -> DecisionMakerIntelligenceResponse:
    person = await _get_person(session, context.tenant.id, person_id)
    before = {"status": person.status.value, "confidence_score": person.confidence_score, "confidence_level": person.confidence_level}
    rows = await session.scalars(select(DecisionMakerEvidence).where(
        DecisionMakerEvidence.tenant_id == context.tenant.id,
        DecisionMakerEvidence.decision_maker_id == person.id,
    ))
    result = calculate_decision_maker_confidence(rows.all())
    person.confidence_score = int(result["score"])
    person.confidence_level = str(result["level"])
    person.status = result["status"]
    person.last_verified_at = datetime.now(UTC)
    session.add(DecisionMakerChange(
        tenant_id=context.tenant.id,
        decision_maker_id=person.id,
        change_type="confidence_reassessed",
        before=before,
        after={"status": person.status.value, "confidence_score": person.confidence_score, "confidence_level": person.confidence_level, "assessment": result},
    ))
    await record_audit_event(
        session, tenant_id=context.tenant.id, actor_user_id=context.user.id,
        action="decision_maker.reassessed", resource_type="decision_maker", resource_id=str(person.id),
        details={"confidence_score": person.confidence_score},
        ip_address=request.client.host if request.client else None,
    )
    await session.commit()
    await session.refresh(person)
    return DecisionMakerIntelligenceResponse.model_validate(person)


@router.patch("/decision-makers/{person_id}/profile", response_model=DecisionMakerIntelligenceResponse)
async def update_decision_maker_profile(
    person_id: UUID,
    payload: DecisionMakerUpdate,
    request: Request,
    context: TenantContext = Depends(require_permission("companies:write")),
    session: AsyncSession = Depends(get_db_session),
) -> DecisionMakerIntelligenceResponse:
    person = await _get_person(session, context.tenant.id, person_id)
    before = {"full_name": person.full_name, "job_title": person.job_title, "role_category": person.role_category.value, "status": person.status.value}
    updates = payload.model_dump(exclude_unset=True)
    if "full_name" in updates and updates["full_name"]:
        person.full_name = updates["full_name"].strip()
        person.normalized_name = normalize_person_name(person.full_name)
    if "job_title" in updates:
        person.job_title = updates["job_title"]
        if "role_category" not in updates:
            updates["role_category"] = classify_job_title(person.job_title)
    if "role_category" in updates and updates["role_category"] is not None:
        from app.models.prospecting import DecisionMakerRole
        person.role_category = DecisionMakerRole(updates["role_category"])
    if "professional_email" in updates:
        person.professional_email = updates["professional_email"]
    if "professional_phone" in updates:
        person.professional_phone = updates["professional_phone"]
    if "status" in updates and updates["status"] is not None:
        from app.models.prospecting import DecisionMakerStatus
        person.status = DecisionMakerStatus(updates["status"])
    after = {"full_name": person.full_name, "job_title": person.job_title, "role_category": person.role_category.value, "status": person.status.value}
    session.add(DecisionMakerChange(tenant_id=context.tenant.id, decision_maker_id=person.id, change_type="profile_updated", before=before, after=after))
    await record_audit_event(session, tenant_id=context.tenant.id, actor_user_id=context.user.id, action="decision_maker.profile_updated", resource_type="decision_maker", resource_id=str(person.id), details={"changed_fields": sorted(updates)}, ip_address=request.client.host if request.client else None)
    await session.commit()
    await session.refresh(person)
    return DecisionMakerIntelligenceResponse.model_validate(person)


@router.post("/decision-makers/{person_id}/evidence/{evidence_id}/mark-stale", response_model=DecisionMakerEvidenceResponse)
async def mark_decision_maker_evidence_stale(
    person_id: UUID,
    evidence_id: UUID,
    request: Request,
    context: TenantContext = Depends(require_permission("companies:write")),
    session: AsyncSession = Depends(get_db_session),
) -> DecisionMakerEvidenceResponse:
    person = await _get_person(session, context.tenant.id, person_id)
    evidence = await session.scalar(select(DecisionMakerEvidence).where(DecisionMakerEvidence.id == evidence_id, DecisionMakerEvidence.tenant_id == context.tenant.id, DecisionMakerEvidence.decision_maker_id == person.id))
    if evidence is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    if evidence.is_current:
        evidence.is_current = False
        session.add(DecisionMakerChange(tenant_id=context.tenant.id, decision_maker_id=person.id, change_type="evidence_marked_stale", before={"evidence_id": str(evidence.id), "is_current": True}, after={"evidence_id": str(evidence.id), "is_current": False}, source_evidence_id=evidence.id))
        rows = await session.scalars(select(DecisionMakerEvidence).where(DecisionMakerEvidence.tenant_id == context.tenant.id, DecisionMakerEvidence.decision_maker_id == person.id))
        result = calculate_decision_maker_confidence(rows.all())
        person.confidence_score = int(result["score"])
        person.confidence_level = str(result["level"])
        person.status = result["status"]
        await record_audit_event(session, tenant_id=context.tenant.id, actor_user_id=context.user.id, action="decision_maker.evidence_marked_stale", resource_type="decision_maker", resource_id=str(person.id), details={"evidence_id": str(evidence.id)}, ip_address=request.client.host if request.client else None)
        await session.commit()
        await session.refresh(evidence)
    return DecisionMakerEvidenceResponse.model_validate(evidence)
