from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import TenantContext, get_tenant_context, require_permission
from app.core.config import get_settings
from app.core.security import create_access_token, hash_password, verify_password
from app.db import get_db_session
from app.models.identity import Membership, MembershipRole, Tenant, User
from app.schemas.identity import (
    CurrentIdentityResponse,
    LoginRequest,
    OrganizationResponse,
    RegisterRequest,
    TokenResponse,
    UserResponse,
    MembershipRoleUpdateRequest,
)
from app.services.audit import record_audit_event

router = APIRouter(prefix="/identity", tags=["identity"])


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    email = str(payload.email).lower()
    existing = await session.scalar(select(User.id).where(User.email == email))
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    tenant = Tenant(name=payload.organization_name.strip(), slug=payload.organization_slug)
    user = User(email=email, full_name=payload.full_name.strip(), password_hash=hash_password(payload.password))
    try:
        session.add_all([tenant, user])
        await session.flush()
        membership = Membership(user_id=user.id, tenant_id=tenant.id, role=MembershipRole.OWNER)
        session.add(membership)
        await record_audit_event(
            session,
            tenant_id=tenant.id,
            actor_user_id=user.id,
            action="identity.registered",
            resource_type="tenant",
            resource_id=str(tenant.id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Email or organization slug already in use") from None
    return TokenResponse(
        access_token=create_access_token(user_id=user.id, tenant_id=tenant.id),
        expires_in=get_settings().access_token_expire_minutes * 60,
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    result = await session.execute(
        select(User, Tenant, Membership)
        .join(Membership, Membership.user_id == User.id)
        .join(Tenant, Tenant.id == Membership.tenant_id)
        .where(User.email == str(payload.email).lower(), Tenant.slug == payload.organization_slug)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid email, password or organization")
    user, tenant, membership = row
    if not user.is_active or not tenant.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email, password or organization")
    await record_audit_event(
        session,
        tenant_id=tenant.id,
        actor_user_id=user.id,
        action="identity.login",
        resource_type="user",
        resource_id=str(user.id),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    return TokenResponse(
        access_token=create_access_token(user_id=user.id, tenant_id=tenant.id),
        expires_in=get_settings().access_token_expire_minutes * 60,
    )


@router.get("/me", response_model=CurrentIdentityResponse)
async def me(context: TenantContext = Depends(get_tenant_context)) -> CurrentIdentityResponse:
    return CurrentIdentityResponse(
        user=UserResponse.model_validate(context.user),
        organization=OrganizationResponse.model_validate(context.tenant),
        role=context.membership.role.value,
    )


@router.get("/members", response_model=list[UserResponse])
async def list_members(
    context: TenantContext = Depends(require_permission("members:read")),
    session: AsyncSession = Depends(get_db_session),
) -> list[UserResponse]:
    result = await session.scalars(
        select(User)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.tenant_id == context.tenant.id)
        .order_by(User.full_name)
    )
    return [UserResponse.model_validate(user) for user in result.all()]


@router.patch("/members/{member_user_id}/role", status_code=status.HTTP_204_NO_CONTENT)
async def update_member_role(
    member_user_id: str,
    payload: MembershipRoleUpdateRequest,
    context: TenantContext = Depends(require_permission("members:roles:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    try:
        target_user_id = UUID(member_user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Member not found") from None
    membership = await session.scalar(
        select(Membership).where(
            Membership.tenant_id == context.tenant.id,
            Membership.user_id == target_user_id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="Member not found")
    if membership.role == MembershipRole.OWNER and payload.role != MembershipRole.OWNER:
        owners = await session.scalars(
            select(Membership).where(
                Membership.tenant_id == context.tenant.id,
                Membership.role == MembershipRole.OWNER,
            )
        )
        if len(owners.all()) <= 1:
            raise HTTPException(status_code=409, detail="The organization must retain at least one owner")
    previous_role = membership.role
    membership.role = payload.role
    await record_audit_event(
        session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="membership.role_updated",
        resource_type="membership",
        resource_id=str(membership.id),
        details={"user_id": str(target_user_id), "from": previous_role.value, "to": payload.role.value},
    )
    await session.commit()
