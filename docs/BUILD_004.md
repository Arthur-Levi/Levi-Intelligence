# Build 004 — Discovery Worker, Company Enrichment & Automated Qualification

## Scope

This build implements the worker and data processing stages on top of the tenant-scoped Build 002/003 model.

### Discovery worker

- Durable queue stored in `prospecting_runs` in PostgreSQL.
- Worker process: `python -m app.workers.discovery`.
- Claims jobs using `SELECT ... FOR UPDATE SKIP LOCKED`.
- States: `queued`, `running`, `completed`, `failed`, `cancelled`.
- Retry policy with exponential backoff and configurable maximum attempts.
- Cancellation immediately marks queued work cancelled; running work records a cancellation request and is stopped at the next safe worker boundary (the current provider request is not force-killed).
- `Idempotency-Key` is required on `POST /api/v1/prospecting-runs`; the same key and same request return the original run, while reuse with a different payload returns HTTP 409.
- Company deduplication is tenant-scoped by normalized domain. Source records are deduplicated by provider external ID where available.

### Apify adapter

Configure `APIFY_API_TOKEN` and `APIFY_ACTOR_ID`. The adapter starts the configured Actor, waits for completion and reads up to 1,000 dataset items. Actor output mapping accepts common business fields (`name`, `website`, `phone`, `email`, `address`, `category`, `url`, `placeId`). Provider-specific input schemas may require adaptation for the selected Actor. Never place the API token in logs or source records.

### Company enrichment

- Normalize company names and HTTP(S) website URLs.
- Normalize domains for tenant-scoped deduplication.
- Verify the homepage with bounded timeouts and no redirect following.
- Reject credentials in URLs and hostnames resolving to non-public IP addresses.
- Extract business emails and phone numbers visibly present in homepage HTML.
- Store verification outcome and captured evidence in `source_records`.
- Do not infer or fabricate contact information. This is not automated personal-profile scraping.

### Automated qualification

`POST /api/v1/companies/{company_id}/qualify/{icp_id}` calculates:

- ICP fit score (industry 40, geography 25, size 20, exclusions 15).
- Data completeness score (existing transparent field-presence baseline).
- Opportunity score (weighted combination of fit and completeness).
- Confidence indicator based on completeness, capped at 0.95.
- Per-criterion match and human-readable reason.

The result is a heuristic prioritization index, not a probability of purchase, revenue forecast or claim of verified intent.

## API additions

- `GET /api/v1/prospecting-runs/{run_id}` — retrieve a run within the active tenant.
- `POST /api/v1/prospecting-runs/{run_id}/cancel` — cancel queued work or request cancellation of running work.
- `POST /api/v1/companies/{company_id}/enrich` — verify website and capture public business contacts.
- `POST /api/v1/companies/{company_id}/qualify/{icp_id}` — calculate ICP fit and opportunity score.

## Operations

1. Configure `.env` using `.env.example`.
2. Run `cd backend && alembic upgrade head`.
3. Start API as usual.
4. Start worker with `docker compose --profile workers up --build -d discovery-worker`.
5. Configure `APIFY_API_TOKEN` and `APIFY_ACTOR_ID` only if Apify is enabled.

The worker polls PostgreSQL directly, so the queue remains durable if the worker restarts. Redis is not required for this queue implementation.

## Acceptance evidence and limitations

- Unit suite: 14 passed, 1 PostgreSQL integration test skipped because a disposable PostgreSQL service was not available in this environment.
- Python compileall: passed.
- Alembic offline SQL generation through `0003_discovery_enrichment`: passed.
- Docker Compose YAML parsed successfully.
- Ruff was not installed in the execution environment; lint was not run.
- No live Apify Actor was run because credentials and the Actor-specific schema were not supplied.
- No real PostgreSQL migration execution was claimed.
- Website verification has DNS-based public-address checks and disables redirects. Production deployment should add network-level egress controls and DNS pinning to fully address DNS rebinding.
- The worker is single-process friendly and uses row locks for multiple workers, but production monitoring, dead-letter operations, per-tenant quotas and queue latency metrics remain future hardening.
