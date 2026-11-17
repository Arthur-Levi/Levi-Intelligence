# Engineering roadmap

## Build 001 — Foundation
Repository scaffold, API health, frontend shell, local infrastructure, product decisions and acceptance gates.

## Build 002 — Identity and tenancy
Authentication, memberships, RBAC, tenant context, audit events, migrations and isolation tests.

## Build 003 — ICP and discovery
ICP profiles, prospecting runs, Apify adapter, organization normalization, deduplication, source records and idempotent workers.

## Build 004 — Website intelligence
Safe fetch service, SSRF defenses, crawl policy, structured page extraction, Lighthouse integration and audit artifacts.

## Build 005 — Decision-maker research
Permitted-source research adapters, professional-role evidence, provenance and confidence; no fabricated identities or personal contact data.

## Build 006 — Opportunity scoring
Versioned dental ICP rubric, evidence-backed score breakdown and calibration dataset.

## Build 007 — Diagnostic studio
Dental diagnostic template, evidence-bound generation, PDF rendering, reviewer workflow and revision history.

## Build 008 — CRM
Deals, stages, activities, tasks, suppression, timeline and pipeline analytics.

## Build 009 — SaaS operations
Billing, quotas, usage metering, tenant administration, observability, backups and deployment hardening.

## Build 003 — ICP & Lead Discovery Engine

- [x] Tenant-scoped ICP profiles and configuration.
- [x] Tenant-scoped prospecting run history with explicit queued status.
- [x] Tenant-scoped company portfolio and domain deduplication.
- [x] Source provenance records for company entries.
- [x] Decision-maker records with evidence/source fields.
- [x] Deterministic, explainable baseline qualification score.
- [x] Composite tenant foreign keys for ICP/run/company/source relationships.
- [x] API permission checks and audit events for create/status operations.
- [ ] Execute migration and cross-tenant API tests against disposable PostgreSQL.
- [ ] Configure and execute the real Apify actor via a durable worker.
- [ ] Add company enrichment, website analysis and evidence confidence scoring.
