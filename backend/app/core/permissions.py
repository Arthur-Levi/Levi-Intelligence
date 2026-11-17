from app.models.identity import MembershipRole

PERMISSIONS: dict[str, frozenset[MembershipRole]] = {
    "organization:read": frozenset(MembershipRole),
    "members:read": frozenset({MembershipRole.OWNER, MembershipRole.ADMIN}),
    "members:manage": frozenset({MembershipRole.OWNER, MembershipRole.ADMIN}),
    "members:roles:manage": frozenset({MembershipRole.OWNER}),
    "billing:manage": frozenset({MembershipRole.OWNER}),
    "diagnostics:read": frozenset(MembershipRole),
    "diagnostics:write": frozenset({MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MEMBER}),
    "settings:manage": frozenset({MembershipRole.OWNER, MembershipRole.ADMIN}),
}


def role_allows(role: MembershipRole, permission: str) -> bool:
    return role in PERMISSIONS.get(permission, frozenset())

# Prospecting resources are tenant-owned; viewer can inspect, members can create/update.
PERMISSIONS.update(
    {
        "icp:read": frozenset(MembershipRole),
        "icp:write": frozenset({MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MEMBER}),
        "prospecting:read": frozenset(MembershipRole),
        "prospecting:write": frozenset({MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MEMBER}),
        "companies:read": frozenset(MembershipRole),
        "companies:write": frozenset({MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MEMBER}),
    }
)
