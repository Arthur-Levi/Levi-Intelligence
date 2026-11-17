# Build 003 — ICP & Lead Discovery Engine

## Scope

Build 003 introduces tenant-owned ICP profiles, prospecting run history, a company portfolio, decision-maker records, source provenance and an explainable baseline qualification score.

## Tenant boundary

Every ICP, prospecting run, company, decision-maker and source record stores `tenant_id`. Every API read/update query scopes by the authenticated `TenantContext.tenant.id`. A prospecting run can only reference an active ICP belonging to the same tenant. A company source history endpoint verifies company ownership before returning source records.

The database migration adds tenant foreign keys, tenant-aware unique/index constraints, and composite foreign keys that prevent an ICP, company, or source record from being linked across organizations even if an application query is incorrectly constructed. PostgreSQL row-level security is a future hardening layer; the current security boundary is server-side query scoping and authorization, not RLS.

## API

- `POST/GET /api/v1/icp-profiles`
- `POST/GET /api/v1/prospecting-runs`
- `POST/GET /api/v1/companies`
- `PATCH /api/v1/companies/{company_id}/status`
- `GET /api/v1/companies/{company_id}/sources`
- `POST/GET /api/v1/companies/{company_id}/decision-makers`

All endpoints require a valid tenant-scoped access token. `POST /prospecting-runs` returns `202 Accepted` and persists a `queued` run. It does not claim that an external discovery actor has executed.

## Discovery provider boundary

`app.integrations.prospecting` defines a provider protocol and explicit Apify adapter configuration boundary. The live Apify actor execution and asynchronous worker are not enabled in this build: they require credentials, actor input/output mapping, retries, rate limits, and operational controls. Requests selecting `provider=apify` are explicitly rejected with HTTP 503 instead of being left in a misleading queued state. No leads are fabricated when the provider is unavailable.

## Qualification

The baseline score is deterministic and explainable. It awards points only for observed fields (website, phone, email, address, industry, employee estimate and source URL). It is not an AI prediction, does not infer revenue or buying intent, and is stored with reasons.

## Migration

Run `alembic upgrade head` against the dedicated Levi Intelligence database. Do not point migrations at another project or production database during development.

## Acceptance notes

Unit-level tenant query and qualification tests are included. Real PostgreSQL migration and cross-tenant API acceptance still require running the dedicated `postgres-test` service. The live Apify adapter and worker remain a subsequent milestone.
