# ADR 0003: Tenant isolation from day one

- Status: Accepted
- Date: 2026-11-16

## Decision
All customer-owned data is tenant-scoped. Authenticated membership establishes tenant context. Service/repository methods require tenant context; database row-level security is planned as defense in depth. Background jobs include tenant identity and re-check authorization at execution.

## Consequences
- Tenant isolation tests are release blockers.
- No public API accepts tenant_id as proof of access.
- Shared global organization intelligence, if introduced, requires a separate legal, privacy and access-control design.
