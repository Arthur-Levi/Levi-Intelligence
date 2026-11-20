# SEC-OPS Gate 1B.1 — Lifecycle, Concurrency & Recovery

## Baseline and architecture inventory

- Required branch: `feature/build-006a-secure-crawler`; initial commit: `829708693d0a7a7747b3bcc172315f11efc82412`; initial tree clean and tracking `origin/feature/build-006a-secure-crawler`.
- Existing CI has `backend`, `postgres-integration` (disposable PostgreSQL 16, synthetic identity, fail-closed preflight, Alembic migration and schema verification), and `frontend` jobs.
- Migrations 0005 and 0006 define tenant-scoped `crawler_jobs`, `(tenant_id, idempotency_key)` uniqueness, composite company FK, attempts check, and lease columns.
- API admission checks tenant membership/permission, selects company by tenant, caps queued jobs at 10,000 globally and 100 per tenant, and uses a PostgreSQL transaction advisory lock. GET and cancel select by `(job_id, tenant_id)`; cancel locks the row.
- The dedicated worker has bounded local concurrency (1–8). Repository claims use the same advisory lock, `FOR UPDATE SKIP LOCKED`, maximum eight active globally and two per tenant. The worker invokes the existing secured crawler transport; this gate does not call external sites.
- State machine: `QUEUED -> RUNNING -> COMPLETED|FAILED|CANCELLED`; expired `RUNNING -> QUEUED` with bounded exponential backoff, or `FAILED` at five attempts. A cancelled queued or running job enters `CANCELLED`. Terminal jobs have no reexecution route.
- Ownership is a tenant, company, job ID and random lease token. The token changes on every claim, while `attempts` is monotonic and capped at five. A result transaction locks the job and company, validates lease, tenant, company and cancellation, then writes the company, source record and terminal job state atomically.
- Idempotency key scope is tenant; payload fingerprint is SHA-256 over company ID and website URL. The unique database constraint is retained for the lifetime of the job; no expiry or purge exists. Same key and fingerprint returns the existing job, including terminal jobs. Different fingerprint returns 409. Reexecution requires a new key.
- There is no PostgreSQL row-level security. The application and repository enforce tenant predicates; composite FK protects job-to-company integrity. A database principal with unrestricted direct SQL access can read other tenants' rows.
- The existing per-process crawler admission and robots bounds remain in place. They do not substitute for the database queue and worker caps.

## Gaps found and corrections

1. An expired running job with a cancellation request could be queued again. Recovery now finalizes it as `CANCELLED`; claims exclude cancellation-requested rows.
2. Lease expiration used `< now`, leaving an exact-boundary gap. Recovery now uses `<= now`; renewal and completion require `> now`.
3. A running cancellation left a window in which a failure could be persisted. API cancellation now revokes the lease and commits `CANCELLED` under the row lock. Failure persistence also rejects cancellation requests; heartbeat rejects them.
4. The API checked idempotency before taking the admission lock but did not recheck inside it. It now rechecks after lock acquisition; insert, audit and commit are within the integrity-error handling block.
5. Repository claims had no optional tenant restriction. `claim_next(tenant_id)` now limits candidates for tenant-bound use; the dedicated trusted worker uses global claims.
6. The existing unit API stub had two scalar answers. It was updated for the additional idempotency query.

## Invariants and semantics

- The advisory lock serializes admissions, recoveries and claims across processes. Row locks and `SKIP LOCKED` prevent a locked job from being claimed. This global serialization is safe but may limit throughput. The exact active cap relies on all writers using this repository; direct SQL writers can bypass it.
- A lease token is a bearer capability. Renewal and completion require matching tenant, job, company, token, `RUNNING` state and unexpired lease. The token is a fencing value; `attempts` gives a monotonic generation. Worker identity is represented by possession of the token; a separately stored worker ID and attempt history table are not present.
- At the exact expiry timestamp the old token is invalid. A recovered claim receives a new token. The old attempt cannot update company or source records.
- Cancellation and completion lock the same job row. Whichever transaction commits first wins: a committed completion yields 409 on later cancel; a committed cancellation prevents later result persistence. The worker can still perform in-flight external HTTP activity before observing cancellation; no rollback of external side effects is claimed.
- Expired attempts back off by `min(60, 2 ** attempts)` seconds. On the fifth expired attempt, the job becomes `FAILED` with `retry_exhausted`. Explicit crawler errors are terminal. A worker crash after claim is recovered on lease expiry; a crash during result commit rolls back atomically; a crash after commit cannot duplicate the result because the job is terminal.
- Pending admission: 10,000 global / 100 per tenant; running: eight global / two per tenant; attempts: five; recovery scan: 32; claim scan: 64; local worker concurrency: 1–8. Admission returns 429 with a stable capacity code. No infinite in-memory queue is introduced.

## Test matrix and evidence

| Control | Real PostgreSQL integration test | Current evidence |
|---|---|---|
| Concurrent claims, repeated disputes | `test_claim_races_and_repeated_disputes`, `test_skip_locked_and_rollback_before_commit` | Awaiting Actions |
| Tenant and attempt fencing | `test_tenant_and_attempt_fencing` | Awaiting Actions |
| Cancellation | `test_cancel_revokes_running_lease_and_prevents_result`, `test_expired_cancel_and_retry_exhaustion` | Awaiting Actions |
| Idempotency and conflict | `test_concurrent_idempotent_admission_and_conflict` | Awaiting Actions |
| Queue and active capacity | `test_tenant_queue_capacity_and_release`, `test_global_and_tenant_active_capacity` | Awaiting Actions |

All integration tests use the existing fail-closed CI preflight and create synthetic rows with explicit cleanup. Concurrent contenders use separate sessions and an event; the lock-holding test coordinates via an open transaction. Lease tests manipulate timestamps rather than waiting for wall-clock expiry. No real websites, commercial APIs or credentials are used.

Local PostgreSQL execution is unavailable: Docker daemon access is denied. The Actions `postgres-integration` job is the required execution venue. Do not interpret a green unit suite as PostgreSQL evidence. Record the workflow run ID, URL, job conclusions and new test counts after push.

## Residual risks and acceptance decision

- **PENDING** until the new PostgreSQL integration tests and all CI jobs complete and their logs confirm execution. No gate control is marked PASS yet.
- Attempt history is represented only by count and current token; there is no durable per-attempt ledger. A separate worker identity field is absent.
- Structured event coverage is incomplete; only claim and API audit events currently exist. Critical transaction safety does not depend on logging.
- No dedicated database tests yet prove simultaneous renewal versus expiration, completion versus cancellation, global queue threshold at 10,000, connection loss, restart, or external side-effect compensation. These controls cannot receive PASS without further evidence.
- The trusted worker claims globally. The optional tenant filter applies when a tenant-bound caller uses the repository; unrestricted SQL access is outside the application isolation model.
