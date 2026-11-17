# Build 002 — Identity, Multi-Tenancy & Core Database

## Delivered foundation

- Email/password registration and login; passwords are Argon2id-hashed and never returned.
- JWT access tokens with `sub` (user) and `tid` (active tenant), expiry and token type.
- Organization creation during signup; tenant membership and owner role.
- Server-side tenant context resolves the token's user + tenant + active membership on every protected request. Client-supplied tenant IDs are not trusted.
- RBAC roles: owner, admin, member and viewer; permissions are centralized in `app/core/permissions.py`.
- SQLAlchemy 2 async models and initial Alembic migration.
- Audit events for registration and successful login; tenant-scoped audit listing.
- PostgreSQL readiness probe and opt-in disposable PostgreSQL integration test.

## API

- `POST /api/v1/identity/register`
- `POST /api/v1/identity/login`
- `GET /api/v1/identity/me`
- `GET /api/v1/identity/members` (owner/admin)
- `PATCH /api/v1/identity/members/{member_user_id}/role` (owner only; prevents removing the last owner)
- `GET /api/v1/organizations/current`
- `GET /api/v1/organizations/audit-events` (current tenant only)

Registration request example:

```json
{
  "full_name": "Example User",
  "email": "user@example.com",
  "password": "a-long-password-of-at-least-12-chars",
  "organization_name": "Example Dental Group",
  "organization_slug": "example-dental"
}
```

## Database tables

- `users`: global account identity, unique normalized email.
- `tenants`: customer organizations, unique slug.
- `memberships`: user-to-tenant relationship and role.
- `audit_events`: append-oriented operation history, always associated with a tenant.

## Security boundaries and known limitations

- Access tokens are short-lived bearer tokens. Refresh-token rotation, email verification, password reset,
  MFA, invitations, session revocation and rate limiting are not included in this build.
- Memberships support multiple tenants at the data-model level; initial registration creates the first tenant. Role assignment is owner-only in this build; inviting an unregistered person and tenant switching are future work.
- Tenant isolation is enforced in protected request context and tenant-qualified queries. PostgreSQL RLS
  is a planned defense-in-depth layer before production workloads.
- Audit records have no update/delete API; stronger database-level immutability and retention policy remain
  production hardening work.
- Do not deploy with the development SECRET_KEY. Set a cryptographically random secret in production.

## Validation status

Unit tests cover password hashing, JWT tenant claims, RBAC and explicit tenant-qualified SQL query shape.
The PostgreSQL integration test is opt-in and only runs when `TEST_DATABASE_URL` is set to a dedicated
`*_test` database. It must be run in an environment with Docker and `asyncpg` installed.
