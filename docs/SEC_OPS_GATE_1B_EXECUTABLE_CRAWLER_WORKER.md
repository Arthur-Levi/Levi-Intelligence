# SEC-OPS Gate 1B — Executable Crawler Worker

**Branch:** `feature/build-006a-secure-crawler`
**Scope:** repository implementation and development runtime preparation
**Decision:** **NO-GO** for Gate 1B acceptance. The repository contains a partial application foundation, but required database lifecycle evidence and least-privilege authorization are missing.

## Previous architecture and gaps

Gate 1A introduced tenant-scoped submissions and worker contracts, but no PostgreSQL adapter or crawler-only executable. Discovery remains a separate provider worker with general database access and is not reused. The synchronous enrichment compatibility endpoint still exists. Gate 1 found no production runtime, egress enforcement, or effective container policy evidence.

Lifecycle intended here: API validates tenant/company and idempotency, commits the durable job and audit event; the worker claims under PostgreSQL coordination; validates the bounded envelope; invokes the existing `verify_and_extract` service; and atomically writes company fields, provenance source evidence, validated result, and terminal status. Cancellation and structured failures have separate terminal writes.

## Implemented worker architecture

`python -m app.workers.crawler` starts a dedicated loop. The Compose `workers` profile now includes `crawler-worker`, configured with a separate `CRAWLER_DATABASE_URL`, concurrency up to eight (development default two), short polling, and a bounded pool. The worker calls the existing `verify_and_extract`; it introduces no HTTP client or crawler implementation. A claim loop has one task per configured slot.

The worker configuration declares non-root UID/GID, read-only root, 16 MiB `/tmp`, no capabilities, no-new-privileges, PID limit 64, 512 MiB memory and one CPU. It declares no host mounts or Docker socket. Compose remains on its default network: dedicated namespace and egress policy are not implemented. These are declarations only; no runtime container was started or inspected.

## Repository adapter and transaction boundaries

`PostgresCrawlerJobRepository` exposes lifecycle methods only. It accepts a SQLAlchemy session factory internally and does not expose a general session to the worker. Claims take a transaction-scoped advisory lock, recover up to 32 expired leases, choose queued rows with `FOR UPDATE SKIP LOCKED`, and set RUNNING, attempt, start, heartbeat, token, and expiration atomically. Tenant/company identity travels with the returned immutable job.

Each heartbeat or terminal write is a separate transaction. Every ownership query matches job ID, tenant ID, company ID, RUNNING state, lease token, and a non-expired lease. Result persistence locks the job and exact tenant/company row, then updates permitted website fields, inserts a source record with crawler-job provenance, stores the validated result, and marks completion in one transaction. Any exception rolls back all of those changes. A stale token or expired lease cannot complete the job. Failure and cancellation writes use the same ownership predicate. Database foreign keys also bind tenant/company identity.

The orchestration checks cancellation every five seconds and renews the lease through the repository heartbeat while crawling. A missed/failed heartbeat cancels the in-process crawl and makes the stale execution unable to persist. The adapter will reject completion after lease expiry or token replacement.

## Worker identity and authorization

The Compose worker requires a distinct `CRAWLER_DATABASE_URL` and does not inherit the API `DATABASE_URL` or `.env` file. No credentials were created or copied. The preferred production comparison is a dedicated PostgreSQL role with grants limited to required tables/columns plus database-enforced row security, versus a narrow authenticated result API. A role alone with unrestricted row access does not prove tenant isolation; an API would add an authorization boundary but requires authentication, availability, and idempotent result semantics.

This development implementation selects the narrow repository adapter with a separately configured connection string. Its protocol and query predicates are not a database authorization boundary. A dedicated role, grants/RLS, credential issuance, and permission verification were not provisioned. This remains a blocking security dependency.

## Queue limits and fairness

Admission serializes capacity decisions with a PostgreSQL advisory transaction lock and enforces 10,000 queued globally and 100 queued per tenant, returning stable `crawler_queue_full` or `crawler_tenant_queue_full` 429 codes. Database uniqueness plus the API conflict recovery preserves idempotency under concurrent key collision; same-key/different-fingerprint remains 409.

Claims coordinate up to eight active leases globally and two per tenant, also under the advisory lock. They scan at most 64 queued candidates ordered by creation time and tenant ID. This is bounded and gives other tenants an opportunity when earlier candidates have filled their slots, but it is not a formal weighted fair scheduler. Payload URL and idempotency bounds remain 500 and 128 characters. Attempts are bounded at five; retry backoff is capped at 60 seconds. Maximum lifetime is bounded only indirectly by leases and retry delay; no explicit absolute job deadline/retention policy is implemented.

## Lease, recovery, cancellation, shutdown

Lease duration is 90 seconds. Claim assigns a fresh UUID token; heartbeat refreshes only a live matching token; result/failure/cancellation writes require that token and expiry. A later claim recovers expired RUNNING rows, requeues below five attempts, and marks exhausted jobs FAILED with `retry_exhausted`. Cancellation is checked before and after crawling by existing orchestration; queued API cancellations become terminal immediately, running cancellation is cooperative. SIGTERM/SIGINT stop new claims and wait for active work, allowing leases to recover if the process is terminated abruptly.

The worker does not impose an explicit absolute job deadline; the crawler service's existing request limits bound individual requests. Process shutdown waits for active tasks, while abrupt termination relies on lease recovery. Database-to-transport cancellation is cooperative at five-second checks and depends on cancellation-safe transport cleanup.

## Runtime controls

Application-level controls retained: existing URL validation, DNSGuard, PinnedResolver, TLS verification, robots policy, admission control, redirect revalidation, response/time bounds, and transport cleanup. They do not enforce host egress.

Database-enforced controls declared in schema/query: tenant-company foreign keys, idempotency uniqueness, attempt check, row locking, token and identity predicates. No database role or RLS policy was installed or tested.

Container-enforced controls are Compose declarations only and were not runtime inspected. Host/operator-enforced egress, network namespace policy, seccomp/AppArmor, effective resource ceilings, Docker daemon settings, and production runtime remain unverified. Production runtime target remains unresolved.

## Tests and validation

Database integration tests were not run. No database service was started. The repository has an opt-in `postgres-test` Compose service backed by tmpfs, but no isolated connection was established or verified during this change. No development or production database was used.

| Command | Result |
|---|---|
| `cd backend && pytest -q tests/test_crawler_execution_boundary.py tests/intelligence/crawler tests/test_discovery_enrichment.py tests/test_prospecting_tenant_isolation.py tests/test_tenant_scoping.py tests/test_security.py` | **204 passed** in 2.81s. |
| Scoped Ruff on adapter, worker, contracts, crawler boundary tests, and migration | **Passed**. Existing endpoint/model files have pre-existing lint findings and were not clean under whole-file Ruff. |
| `cd backend && mypy app/crawler_jobs app/workers/crawler.py` | **Passed**, no issues in 5 source files. |
| `cd backend && alembic history` | **Passed**, `0006_crawler_worker_leases` is head. No database was contacted. |
| `git diff --check` | **Passed**. |

The requested lifecycle adversarial tests (PostgreSQL concurrent claims, crash/recovery, lease expiry, stale completion, retry exhaustion, queue saturation/fairness, partial transaction rollback, and restart) have not been added or executed. Existing tests are synthetic and do not establish PostgreSQL semantics.

## Limitations and remaining requirements

1. Implement and test periodic heartbeat renewal, absolute lifetime bounds, and active cooperative cancellation.
2. Add deterministic adapter and queue tests plus full synthetic lifecycle tests; run database tests only against a verified disposable DB.
3. Provision and verify a dedicated worker database role with least privilege and tenant/company enforcement, or implement a narrowly authenticated result API.
4. Prove cross-process capacity/admission behavior, concurrent idempotency, and starvation behavior against PostgreSQL.
5. Review worker completion/failure exception handling and ensure storage failures are not misclassified as crawler failures.
6. Select production runtime and operator; implement and inspect deny-by-default egress and effective container/host resource policy.
7. Remove or deprecate the synchronous compatibility route only after client migration and deployed worker evidence.

No production readiness, infrastructure isolation, effective egress enforcement, or SEC-OPS Gate 1 approval is claimed.
