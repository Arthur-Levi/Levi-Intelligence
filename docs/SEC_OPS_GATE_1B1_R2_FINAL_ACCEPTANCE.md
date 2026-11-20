# SEC-OPS Gate 1B.1 R2 — final acceptance evidence

## Baseline

Required branch: `feature/build-006a-secure-crawler`; R2 starting SHA: `df6079b0a6638a0077acd44565319726579bfc31`. At inspection, the tree already contained uncommitted attempt model, Alembic 0007 and schema-preflight edits; this work preserved and completed them. Previous Actions run [44751050356](https://github.com/Arthur-Levi/Levi-Intelligence/actions/runs/44751050356) passed backend (214 tests), PostgreSQL 16 migration and 13 integration tests, and frontend build. The previous [Gate 1B.1 report](SEC_OPS_GATE_1B1_LIFECYCLE_CONCURRENCY_RECOVERY.md) documents the proved invariants and remaining gaps.

Architecture: API and repository use a shared advisory lock for admission and claims; jobs use tenant predicates, row locks and random bearer lease tokens. Terminal result, source record and company update share a transaction. PostgreSQL row-level security is absent. Current limits: global pending 10,000, tenant pending 100, global active 8, tenant active 2, maximum attempts 5, recovery batch 32, claim scan 64, local worker concurrency 1–8.

## Changes and storage policy

Alembic 0007 adds `crawler_job_attempts` with tenant/job composite FK, unique generation, worker ID, claim time, renewal summary, terminal outcome and sanitized failure code. The unique `(tenant_id, job_id, generation)` index serves ledger lookup and ordering; a redundant prefix index was removed. One row is inserted in the same transaction as each claim. Renewals update a count and timestamp; they do not create heartbeat rows. Recovery and terminal transitions update the current row. The model stores neither lease token nor result payload. Legacy jobs retain their existing state and acquire a ledger row on their next claim. The ledger is retained for the job's lifetime and deleted by FK cascade with the job. At most five attempt rows per job; no automatic purge currently exists, so job retention needs a separate policy before production scale.

Diagnostic JSON lifecycle events use an allowlist of fields and outcomes. Admission and cancellation also have existing durable audit records; claim/retry/terminal attempt outcomes are durable in the ledger. Diagnostic events are best effort and can precede transaction commit; they are not transactional audit proof. Logger failures are caught. No URLs, tokens, credentials, raw result data or exception text are accepted by the event API.

## Acceptance matrix

| Control | Implementation | Test / environment | Evidence | Decision |
|---|---|---|---|---|
| Concurrent claim safety | `crawler_jobs/postgres.py` | `test_claim_races_and_repeated_disputes`, PostgreSQL 16 CI | Run 44751050356 | PASS |
| Tenant isolation | API/repository tenant predicates, composite FK | `test_tenant_and_attempt_fencing`, PostgreSQL 16 CI | Run 44751050356 | PASS for application boundary |
| Lease fencing | `crawler_jobs/postgres.py` | `test_tenant_and_attempt_fencing`, PostgreSQL 16 CI | Run 44751050356 | PASS |
| Idempotency | API uniqueness and fingerprint | `test_concurrent_idempotent_admission_and_conflict`, PostgreSQL 16 CI | Run 44751050356 | PASS |
| Cancellation | API row lock and lease revocation | `test_cancel_revokes_running_lease_and_prevents_result`, PostgreSQL 16 CI | Run 44751050356 | PASS for DB state |
| Crash recovery | Expiry, bounded backoff | `test_expired_cancel_and_retry_exhaustion`, PostgreSQL 16 CI | Run 44751050356; no killed worker process test | BLOCKED |
| Connection loss | Pool pre-ping and expiry | `test_real_connection_loss_after_claim_commit_recovers`, disposable PostgreSQL 16 CI | Run 44754873469, real backend termination after claim; other failure points absent | BLOCKED overall |
| Worker restart | Lease expiry route | No process restart integration test | Unverified | BLOCKED |
| Attempt ledger | Alembic 0007, claim/recovery/terminal writes | `test_attempt_ledger_retry_fencing_and_rollback`, `test_attempt_ledger_api_cancellation`, PostgreSQL 16 CI | Run 44754873469 | PASS for new claims; legacy attempts are not backfilled |
| Structured observability | `crawler_jobs/events.py`, attempt ledger | `test_structured_event_allowlist_and_redaction`, unit; ledger tests, PostgreSQL 16 CI | Run 44754873469; no end-to-end event coverage assertion | BLOCKED |
| Global queue ceiling | API 10,000 count | No real 10,000-row threshold test | Unverified | NOT MEASURED |
| Per-tenant queue ceiling | API 100 count | `test_tenant_queue_capacity_and_release`, PostgreSQL 16 CI | Run 44751050356 | PASS |
| Active concurrency limits | Repository count under advisory lock | `test_global_and_tenant_active_capacity`, PostgreSQL 16 CI | Run 44751050356 | PASS |
| External side-effect semantics | Lease fencing at result commit | No synthetic HTTP loss/duplicate delivery integration test | Arbitrary external effects need downstream idempotency or compensation | BLOCKED |
| Backpressure | API 429 on pending limits | Tenant threshold test; global threshold unverified | Run 44751050356, partial | BLOCKED |
| Recovery after saturation | Expiry/retry logic | No full saturation recovery test | Unverified | BLOCKED |

## Benchmark and regression

See [capacity baseline](LEVI_INTELLIGENCE_CAPACITY_BASELINE.md). The manual workflow produces a small JSON artifact retained seven days. No R2 capacity values are available until it executes. Synthetic direct inserts only measure subsequent claim and completion paths; they cannot prove admission throughput or the global queue boundary. No production capacity or performance SLO is asserted.

Local R2 regression: 12 focused crawler unit tests passed; Ruff passed; mypy passed for changed backend modules; frontend build passed; `git diff --check` passed. The full 216-test unit suite reached `tests/test_health.py::test_liveness_returns_ok` (91%) and timed out after 120 seconds in the local Python 3.14 environment. Actions [run 44754873469](https://github.com/Arthur-Levi/Levi-Intelligence/actions/runs/44754873469) passed backend Ruff and 216 unit tests on Python 3.12, disposable PostgreSQL 16 identity preflight, Alembic 0007 schema verification, all 16 PostgreSQL integration tests (13 preserved plus 3 new), and frontend build. Local Docker daemon access remains denied. The manual benchmark workflow has not been dispatched; capacity data are **NOT MEASURED**.

## Residual risks and decision

**FAIL** pending full mandatory correctness evidence and bounded capacity measurements. Real disconnection coverage addresses only post-claim connection loss; connection failures during transactions and acknowledgment ambiguity remain untested. Worker process termination/restart and synthetic external side effects remain untested. Direct SQL with unrestricted privileges can bypass application tenant predicates and limits. No arbitrary HTTP effect can be rolled back by the database. The advisory lock may constrain throughput but has not been measured. Do not begin Gate 1B.2 or declare production readiness.
