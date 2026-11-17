# Levi Intelligence

**Business Acquisition Platform** — discover, understand, qualify and acquire B2B customers.

Initial vertical: dental clinics in Brazil. Product architecture is multi-tenant and sector-configurable; dental is the first validation profile, not a hard-coded product boundary.

## Status

Builds 001–004 implemented as an evolving development foundation. PostgreSQL runtime validation and production hardening remain required; this is not yet a production-ready release.

## Architecture

- `backend/`: Python 3.12, FastAPI, Pydantic Settings, SQLAlchemy 2 and Alembic.
- `frontend/`: React + TypeScript + Vite.
- `infra/`: local PostgreSQL and Redis services.
- `docs/`: product decisions, architecture and acceptance criteria.

The backend is organized as a modular monolith first. Agent execution is asynchronous and isolated behind ports/adapters; services can be extracted later only when measured scale or ownership requires it.

## Local development

1. Copy `.env.example` to `.env` and set strong local-only secrets.
2. Start dependencies: `docker compose up -d postgres redis`.
3. Backend: `cd backend && python -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]' && uvicorn app.main:app --reload`.
4. Frontend: `cd frontend && npm install && npm run dev`.

API docs: `http://localhost:8000/docs`; health: `/health/live`; readiness: `/health/ready`.

## Non-negotiable engineering rules

1. Tenant isolation is enforced server-side; never trust a tenant ID supplied by a browser without membership authorization.
2. Personal data processing requires documented purpose, lawful-basis assessment, minimization, retention controls and rights workflows under LGPD.
3. Publicly accessible information is not automatically unrestricted for reuse. Respect source terms, robots policies where applicable, rate limits and provider contracts.
4. Every material claim in a lead profile or diagnostic must retain source, retrieval time and verification status. AI-generated inference must never be presented as fact.
5. No unsolicited bulk outreach. Outreach remains reviewable and subject to applicable law, platform terms and suppression/opt-out controls.
6. External side effects require explicit authorization and auditable execution.
7. No destructive tests against preserved databases; tests use disposable, synthetic environments.
8. A feature is complete only when code, tests, documentation, security review and acceptance evidence exist.

## Initial build sequence

1. Foundation and tenant-aware data model.
2. Lead discovery adapters (Apify first), deduplication and provenance.
3. Decision-maker research using permitted sources only.
4. Evidence-grounded diagnostic generation.
5. CRM pipeline and task automation.
6. Evidence-grounded diagnostic generation.
7. CRM pipeline and task automation.
8. Billing, quotas, observability, hardening and production deployment.


## Build 002 — Identity & Multi-Tenancy

The API now includes registration/login, tenant-scoped JWT identity, organization membership, RBAC,
SQLAlchemy models, an Alembic migration and tenant-scoped audit events. See
[`docs/BUILD_002_IDENTITY.md`](docs/BUILD_002_IDENTITY.md) for API contracts and security boundaries.

### Database setup

```bash
docker compose up -d postgres redis
cd backend
cp ../.env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

Run unit tests with `cd backend && pytest -q`. The PostgreSQL isolation test is opt-in and uses a
dedicated ephemeral database; instructions are in `backend/tests/integration/README.md`.


## Build 003 — ICP & Lead Discovery

See [`docs/BUILD_003.md`](docs/BUILD_003.md) for the tenant-scoped ICP, prospecting run, company portfolio, source provenance, qualification baseline and API endpoints.

### Build 003 API quick start

After applying migrations, authenticate using the Build 002 identity endpoints and pass the returned bearer token to the Build 003 routes. Create an ICP profile first, then create a prospecting run against that ICP, and add discovered/verified companies to the tenant portfolio. Runs are persisted as durable database-backed jobs. Start the discovery worker with `docker compose --profile workers up --build -d discovery-worker`. Configure `APIFY_API_TOKEN` and `APIFY_ACTOR_ID` in `.env` to enable Actor execution. Every create-run request must include an `Idempotency-Key` header.


## Build 004 — Discovery Worker, Enrichment & Qualification

Build 004 adds a PostgreSQL-backed durable work queue, an asynchronous polling worker, retry/backoff, cancellation requests, tenant-scoped idempotency, Apify Actor integration, company normalization, safe website verification, public business contact extraction, evidence capture and explainable ICP fit scoring. See [`docs/BUILD_004.md`](docs/BUILD_004.md).

Apply the new migration before starting the worker:

```bash
cd backend
alembic upgrade head
cd ..
docker compose --profile workers up --build -d discovery-worker
```

The queue is the database itself; Redis is not required for discovery job durability in this build. Apify execution requires a valid Actor ID and token. Website verification refuses non-HTTP(S), credential-bearing, non-public DNS targets and does not follow redirects. It extracts only publicly displayed business contact details from the company homepage; it is not a personal-data enrichment or people-scraping feature.

## Build 005 — Decision Maker Intelligence

Build 005 adds tenant-scoped decision-maker confidence, professional evidence records, profile-change history, source freshness, and an authorized-provider contract. See `docs/DECISION_MAKER_INTELLIGENCE.md` for source policy, LGPD safeguards, scoring, endpoints, and current connector limitations.

Apply the new migration with `cd backend && alembic upgrade head` after configuring a dedicated PostgreSQL database. The migration chain now ends at `0004_decision_maker_intelligence`.
