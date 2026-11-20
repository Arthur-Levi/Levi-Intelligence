from uuid import uuid4

from app.core.permissions import role_allows
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.models.identity import MembershipRole


def test_password_hash_is_not_plaintext_and_verifies() -> None:
    password = "correct-horse-battery-staple-42"
    encoded = hash_password(password)
    assert encoded != password
    assert verify_password(password, encoded)
    assert not verify_password("wrong-password", encoded)


def test_access_token_contains_user_and_tenant_scope() -> None:
    user_id, tenant_id = uuid4(), uuid4()
    payload = decode_access_token(create_access_token(user_id=user_id, tenant_id=tenant_id))
    assert payload["sub"] == str(user_id)
    assert payload["tid"] == str(tenant_id)
    assert payload["type"] == "access"


def test_rbac_permission_matrix() -> None:
    assert role_allows(MembershipRole.OWNER, "billing:manage")
    assert not role_allows(MembershipRole.ADMIN, "billing:manage")
    assert role_allows(MembershipRole.VIEWER, "organization:read")
    assert not role_allows(MembershipRole.VIEWER, "diagnostics:write")
    assert not role_allows(MembershipRole.MEMBER, "unknown:permission")
