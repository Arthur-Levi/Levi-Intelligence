# SEC-OPS Gate 1A — Dedicated Crawler Execution Boundary

**Repository:** `Arthur-Levi/Levi-Intelligence`\
**Branch:** `feature/build-006a-secure-crawler`\
**Scope:** architectural separation and application contracts; no production runtime enforcement\
**Decision:** **CONDITIONAL PASS** for the boundary design and repository implementation; **NO-GO** for deployment or infrastructure acceptance

## Decision and scope

Gate 1A establishes a tenant-scoped durable crawler-job contract, an asynchronous
API submission/status/cancellation surface, and a worker orchestration port. It
does not establish an executable isolated worker deployment. The existing
`POST /companies/{company_id}/enrich` route remains as a compatibility path and
still performs synchronous crawling. New clients can use
`POST /companies/{company_id}/enrich/jobs`; that route returns `202` with a job
identifier. The asynchronous route must not be treated as operational until a
worker repository adapter, worker process, and authorized deployment identity
are supplied and validated.

This gate does not repeat Gate 1's host/runtime audit. It accepts that Gate 1
found no production target and no evidence of effective network or container
enforcement. No live network request, Docker operation, database service, host
policy, or persistent volume was used or changed here.

## Current architecture discovered

### Company enrichment

Before Gate 1A, `POST /companies/{id}/enrich` selected a company within the
authenticated tenant, then awaited `verify_and_extract()` inside the API
request. That service validates the URL, calls `SecureHTTPTransport`, and
persists extracted contact data, website status, source evidence, and an audit
event through the request's SQLAlchemy session. Unsafe URL/DNS failures map to
422; other crawl failures map to 502.

`SecureHTTPTransport` remains the only transport implementation. It retains URL
validation, `DNSGuard`, `PinnedResolver`, TLS verification, robots checks,
admission control, redirect revalidation, response byte/time limits, and
cleanup. Gate 1A introduces no alternate HTTP client or crawler implementation.

### Discovery queue

Separately, prospecting runs are persisted to `prospecting_runs`. The API
enqueues them, and `app.workers.discovery` claims one row with
`FOR UPDATE SKIP LOCKED`, calls a configured discovery provider, and persists
discovered companies and their source records. It is provider-oriented rather
than crawler-only, has no crawler job contract, and retains Gate 1's noted gaps
around queue capacity, stale claims, and active cancellation. It is not reused
as the crawler worker.

The discovery worker receives a general `DATABASE_URL` via Compose. That access
pattern is not acceptable as the final crawler worker identity. PostgreSQL
remains the current development persistence target; Redis is not used by this
queue.

## Target architecture

```text
Tenant-authorized API
  ├─ validates company and tenant ownership
  ├─ persists a bounded crawler job and audit event
  └─ returns job ID/status (202)
          │ durable queue / narrow repository port
          ▼
Dedicated crawler worker (not yet deployed)
  ├─ claims one authorized job
  ├─ validates the job envelope
  ├─ invokes app.services.company_enrichment.verify_and_extract
  ├─ writes job result, company changes and source evidence atomically
  └─ records terminal state and releases resources
```

The worker is specified against `CrawlerJobRepository`, not an application-wide
`AsyncSession` or arbitrary SQL interface. The required adapter operations are
limited to claim, cancellation check, result persistence, structured failure,
and cancellation persistence. The result operation must authorize the exact
`tenant_id` and `company_id` from the claimed job, update the company, insert
website source evidence, and mark the job complete in one transaction. The
worker must not receive API signing keys, provider credentials, unrelated
application secrets, Docker socket access, or general-purpose DB credentials.
No such least-privilege adapter or credential has yet been implemented.

## API and compatibility

- `POST /companies/{company_id}/enrich/jobs` requires `companies:write`, checks
  company ownership in the active tenant, and requires an `Idempotency-Key`.
  It stores an auditable job and returns `202` with job ID/status.
- `GET /crawler-jobs/{job_id}` requires `companies:read` and scopes lookup by
  both job ID and authenticated tenant ID. Cross-tenant and nonexistent jobs
  return the same 404 response.
- `POST /crawler-jobs/{job_id}/cancel` requires `companies:write`. Queued jobs
  become cancelled immediately; running jobs receive a cooperative cancellation
  request. A terminal job cannot be cancelled.
- The existing `POST /companies/{company_id}/enrich` remains synchronous and
  retains its `CompanyResponse` behavior for compatibility. It must be deprecated
  and removed only after the asynchronous worker path has deployment evidence
  and clients have migrated.

Idempotency is tenant-scoped. Repeating a key with the same company and URL
returns the existing job; reusing it for a different request returns 409. The
job envelope contains only job, tenant, company, URL and idempotency identifiers.
It excludes user-supplied crawl options and credentials. Worker payload limits
are enforced by the Pydantic contract; queue capacity is not yet bounded.

## Job and result contracts

`CrawlerJobInput` is immutable, rejects unknown fields, and bounds URL length to
500 characters and idempotency key length to 128. Identity values are UUIDs.
The worker validates HTTP(S) URL syntax before invoking the existing enrichment
service; private/reserved address decisions remain in the existing URL/DNS
controls and ultimately require runtime egress enforcement too.

`CrawlerJobResult` permits only `verified`, `reachable_non_html`, or
`http_error`, an HTTP status code, checked timestamp, and at most ten email and
ten phone values. Failures are stored as stable codes (`invalid_job`,
`unsafe_website`, `crawler_error`), not raw exception text or response bodies.
Cancellation is persisted separately. The repository adapter must serialize
timestamps as JSON-compatible ISO values and validate them when reading.

## Identity, authorization, and audit

The API derives the tenant and actor from `TenantContext`; callers cannot choose
the tenant in the request body. Job creation stores tenant, company, creator,
URL, idempotency key, and request fingerprint. The relational schema includes a
composite tenant/company foreign key. API status/cancel queries constrain both
job and tenant. Job enqueue and cancellation requests create tenant audit
events.

Worker-side authorization is a repository-port invariant: claim returns a
validated job and each write is scoped by job ID, tenant ID, and company ID.
The in-memory synthetic tests verify identity preservation and the API query
shape, but database row authorization and transaction behavior have not been
validated against PostgreSQL in this gate.

## Security invariants

- No crawler-specific transport or security policy is duplicated. The worker's
  default callable is `verify_and_extract` from the existing service.
- The worker port exposes only claim, cancellation check, and narrowly scoped
  terminal persistence operations.
- Job payloads reject unknown keys and have explicit length/cardinality bounds.
- Result and failure contracts exclude arbitrary data, credentials, and raw
  exception messages.
- Worker task cancellation is caught for terminal cancellation persistence and
  then re-raised so process shutdown is not swallowed.
- These are code-level invariants. They do not prove network, OS, database, or
  deployment isolation.

## Runtime decision and deployment assumptions

**Development target:** the repository's PostgreSQL-backed Docker Compose
architecture. Compose currently declares PostgreSQL, Redis, an opt-in discovery
worker, and an opt-in test PostgreSQL service. It does not declare an API
service or crawler worker. This gate does not modify Compose. A dedicated
crawler service may be evaluated for development after its repository adapter
and bounded local configuration exist; local Compose settings cannot prove
production enforcement.

**Production target:** unresolved. Repository evidence does not select
Kubernetes, a managed container service, a VM/systemd service, or another
runtime. An operator must select the target and own the network and host policy
before deployment-specific controls are implemented. The next stage must also
choose a narrow result-write mechanism, such as a narrowly privileged database
role with row/column restrictions or an authenticated result API. This gate
does not guess either choice.

Future runtime requirements remain:

- dedicated non-root UID/GID, no unnecessary capabilities, and no privilege
  escalation;
- read-only root filesystem, bounded temporary storage, no host mounts, and no
  Docker socket;
- dedicated network namespace and operator-enforced deny-by-default egress;
- explicit approved DNS path, blocked loopback/private/reserved/link-local/
  metadata/internal/control-plane/Docker destinations, and narrowly restricted
  access to required application services;
- effective CPU, memory, swap, PID, file descriptor, socket, and writable
  storage limits; bounded global/per-tenant queue admission and concurrency;
- leases, heartbeats, stale-job recovery, bounded retry/dead-letter behavior,
  and graceful shutdown in the selected runtime.

Codespaces can run synthetic contract tests and static lint/type checks. The
repository does not have permission/evidence to verify effective container
settings, host egress policy, Docker daemon configuration, or production
cgroups. Compose parsing can verify syntax only. Runtime inspection in the
selected deployment and operator evidence are required for those controls.

## Test evidence

Synthetic tests added for API job creation, idempotency fingerprinting, tenant
and company identity preservation, tenant-scoped lookup and cross-tenant 404,
bounded/strict payload validation, URL syntax rejection, result persistence
through the narrow port, structured failure handling, cancellation before and
during execution, and default invocation of the existing company enrichment
service. Tests use in-memory fakes and do not access PostgreSQL or the network.

Validation evidence:

| Command | Result |
|---|---|
| `pytest -q tests/test_crawler_execution_boundary.py tests/intelligence/crawler tests/test_discovery_enrichment.py tests/test_prospecting_tenant_isolation.py tests/test_tenant_scoping.py tests/test_security.py` | **204 passed** in 3.56s |
| `ruff check app/crawler_jobs tests/test_crawler_execution_boundary.py alembic/versions/0005_crawler_execution_jobs.py` (from `backend/`) | **Passed**, all checks passed |
| `mypy app/crawler_jobs app/api/v1/endpoints/prospecting.py app/models/prospecting.py app/models/__init__.py app/schemas/prospecting.py` | **Passed**, no issues in 7 files |
| `alembic history` | **Passed**, `0005_crawler_execution_jobs` is the head after `0004_decision_maker_intelligence` |
| `git diff --check` | **Passed** |
| Full `pytest -q` | **Not completed**: after fixing a direct-call compatibility test, the run stalled at existing `tests/test_health.py::test_liveness_returns_ok`; that test also stalled when run alone. No database or external service was started. |
| Full `ruff check app tests` | **Failed on existing lint debt** across untouched files; changed/new worker contract, tests, and migration pass scoped Ruff. |
| Full `mypy app` | **Failed with 8 existing errors** in `app/api/v1/endpoints/identity.py` and `decision_makers.py`; changed files pass scoped Mypy. |

Passing synthetic and scoped static checks are not runtime enforcement evidence.

## Unresolved decisions and remaining dependencies

1. Production runtime and operator/network-policy owner.
2. Narrow worker identity and result-write mechanism; current API DB credentials
   must not be copied into a crawler container.
3. Concrete repository adapter and executable crawler process, including a
   bounded claim loop.
4. Queue capacity globally/per tenant, fairness, and concurrency limits.
5. Lease/heartbeat, stale-job recovery, retry/dead-letter policy, and shutdown
   semantics in the selected database/runtime.
6. Worker integration tests against disposable infrastructure and effective
   runtime inspection evidence.
7. Client migration and retirement date for the synchronous compatibility
   route.

## Acceptance

**CONDITIONAL PASS** for Gate 1A's repository-level architectural boundary and
safe compatibility path. **NO-GO** for production deployment and infrastructure
acceptance. The asynchronous API currently records jobs but there is no worker
adapter/process to claim them. The legacy route still crawls synchronously.
Neither the code contracts nor the local Compose architecture demonstrate an
enforced egress, container, identity, or resource boundary.
