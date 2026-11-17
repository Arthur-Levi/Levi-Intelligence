from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import TenantContext, get_tenant_context
from app.db import get_db_session
from app.schemas.identity import OrganizationResponse

router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.get("/current", response_model=OrganizationResponse)
async def current_organization(
    context: TenantContext = Depends(get_tenant_context),
) -> OrganizationResponse:
    return OrganizationResponse.model_validate(context.tenant)


@router.get("/audit-events")
async def list_audit_events(
    context: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
):
    from app.models.identity import AuditEvent

    result = await session.scalars(
        select(AuditEvent)
        .where(AuditEvent.tenant_id == context.tenant.id)
        .order_by(AuditEvent.created_at.desc())
        .limit(100)
    )
    return [
        {
            "id": str(event.id),
            "action": event.action,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "details": event.details,
            "created_at": event.created_at,
        }
        for event in result.all()
    ]

