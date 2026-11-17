from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db_session
from app.core.permissions import role_allows
from app.core.security import decode_access_token
from app.models.identity import Membership, MembershipRole, Tenant, User
from app.core.permissions import role_allows

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class TenantContext:
    user: User
    tenant: Tenant
    membership: Membership


async def get_tenant_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_db_session),
) -> TenantContext:
    unauthorized = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication")
    if credentials is None:
        raise unauthorized
    try:
        payload = decode_access_token(credentials.credentials)
        user_id, tenant_id = UUID(payload["sub"]), UUID(payload["tid"])
        if payload.get("type") != "access":
            raise unauthorized
    except (jwt.PyJWTError, KeyError, ValueError):
        raise unauthorized from None

    result = await session.execute(
        select(User, Tenant, Membership)
        .join(Membership, Membership.user_id == User.id)
        .join(Tenant, Tenant.id == Membership.tenant_id)
        .where(
            User.id == user_id,
            User.is_active.is_(True),
            Tenant.id == tenant_id,
            Tenant.is_active.is_(True),
        )
    )
    row = result.one_or_none()
    if row is None:
        raise unauthorized
    user, tenant, membership = row
    return TenantContext(user=user, tenant=tenant, membership=membership)


def require_permission(permission: str):
    async def dependency(context: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        if not role_allows(context.membership.role, permission):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return context

    return dependency
