# SEC/OPS Gate 1B.1-R3: final acceptance

## Revision and environment

- Branch: `feature/build-006a-secure-crawler`; no merge or rebase of `main`.
- Executable R3 evidence revision: `349bad9889dc818d04accd54b8d7d918b68228fd`.
- PostgreSQL: **16.16** (`16.16-1.pgdg13+1`), isolated container with a
  512 MiB tmpfs; Alembic head 0007 and test identity verified before runs.
- Runner: Python 3.12 test image, two CPU limit, 512 MiB memory limit;
  synthetic `example.test` identifiers only. No real website or production
  service was contacted.
- Prior R2.1 12-run capacity matrix was not repeated.

## Test inventory and failure scenarios

| Evidence | Executed result | Invariants observed |
|---|---:|---|
| R3-A `test_crawler_fault_framework.py` | 10 passed | Ten deterministic boundary faults; before-commit rollback versus post-commit client error |
| R3-B `test_crawler_worker_recovery_r3.py` | 7 passed | Killed worker after claim and during work; lease fencing; competing reclaims; five-attempt exhaustion; cancellation races |
| R3-C `test_crawler_db_failures_r3.py`, `test_crawler_admission_r3.py` | 12 passed | Connection interruption, database refusal/restoration, pool timeout, ambiguous outcome reconciliation, exact queue boundaries and API process race |
| R3-D `test_crawler_event_trace_r3.py`, `test_crawler_external_effect_r3.py` | 2 passed | Correlated event trace and synthetic external effect replay |
| Preserved PostgreSQL integration tests | 16 passed | Existing lifecycle, tenant, migration, transaction and capacity controls |
| Complete PostgreSQL integration suite | **47 passed in 7.19 s** | All of the above on real PostgreSQL 16 in CI run `44816105554` |
| Backend unit suite | **222 passed in 2.26 s** | Existing backend behavior and event schema in the same CI run |

Each new integration operation has an explicit 2–15 second timeout; suite
timeouts are 60–120 seconds. Child processes are killed and awaited in
`finally`. The R3-C 9,999-row fixture is inserted in 500-row batches and
removed in `finally`; only final decisions call the real API admission code.

## Integrity and recovery decision basis

The R3-A real-transaction assertions show no attempt survives a failed claim
before commit; a post-commit client error leaves exactly one durable attempt.
During result persistence, interrupted transactions leave no source and no
completed job. The ambiguous result-ack case reads the completed job, completed
attempt and one source from a fresh session before any retry. No test reports
success for an unavailable database or an exhausted pool.

R3-B kills a real child process after a committed claim and during synthetic
processing. In both cases the job remains running until a controlled lease
expiry, then the original attempt becomes `expired`, a bounded retry is
scheduled, and the next attempt completes with one source. Old heartbeat,
result and failure writes are rejected after the new claim. Four contenders
recover two expired jobs with exactly one current attempt per job. Five
expirations yield five ledger generations and terminal `retry_exhausted`, with
no sixth claim. Cancellation races against claim and heartbeat leave a
cancelled terminal job, revoked lease and no source. These checks prove no
silent disappearance, stale-token acceptance, double completion, duplicate
committed result, incorrect generation or inconsistent terminal state in the
tested cases.

R3-C closes the exact admission thresholds through the application: one of
four distinct requests wins the tenant 99→100 slot; same-key contenders all
receive one logical job; two tenants race at global 9,999→10,000 and only one
is accepted; a further distinct request receives 429, while the winning key
still returns its existing job. Two independent API subprocesses also race a
tenant's last slot with one accepted and one rejected request. Admission
racing two claims keeps tenant queued count at or below 100 and global running
count at two. Preserved PostgreSQL tests exercise the global 8-active and
tenant 2-active limits, tenant-scoped operations and cross-tenant idempotency
namespace. The global advisory transaction lock and all limits are unchanged.

## Event traceability and external effects

The R3-D trace test reconstructs admission, claim, attempt creation (claim
event plus ledger row), renewal, expiry, queued recovery, retry, cancellation,
completion and terminal failure for three jobs. The diagnostic event schema
contains only event type, timestamp, tenant/job/attempt/worker IDs, outcome and
correlation ID. Recovery and retry events now include the ended attempt and
worker IDs; the ledger resolves attempt generation. The test verifies that no
URL or lease token appears. Diagnostic events are best effort and may precede
transaction commit; the durable attempt ledger and job rows are authoritative.

The synthetic receiver is called twice across a lost lease and retry, applies
one keyed idempotent operation, and leaves one committed database source. This
separately demonstrates a unique committed result, an idempotent test receiver
and the possibility of repeated calls. Arbitrary HTTP effects remain **at
least once** and need downstream idempotency or compensation. No general
exactly-once external execution claim is made.

## Resource ceilings, limitations and blockers

The Python test container had a hard 512 MiB memory limit; a sampled R3-D
integration run used 91.7 MiB of 512 MiB. A sampled disposable-database
connection count was one while the suite ran; this is a snapshot, not an
instantaneous maximum. Test concurrency is bounded at four local contenders
or two API subprocesses. The earlier R2.1 capacity probes observed at most
9–10 PostgreSQL connections under eight workers, below the 12-connection
operational ceiling; this gate did not rerun that matrix. R3-C's dedicated
one-connection pool test explicitly verifies checkout timeout and recovery.

PostgreSQL row-level security is absent. Application queries and composite
foreign keys enforce the tested tenant boundary, while a privileged SQL
principal can bypass that boundary and application limits. The post-commit
fault proves a client-visible ambiguous outcome after a durable commit but
does not prove actual network packet loss at the commit acknowledgment. The
connection tests do interrupt real asyncpg sockets at selected SQL boundaries.
No unresolved critical integrity defect was observed. These limitations do
not grant production crawler capacity or external side-effect guarantees.

## Regression, CI and final decision

- `ruff check app tests`: passed.
- Mypy on `app/crawler_jobs/postgres.py` and all nine new/affected test and
  child modules: passed, no issues in ten source files.
- `npm run build` in `frontend/`: passed (TypeScript and Vite).
- `git diff --check`: passed.
- Feature-branch CI: R3-A run `44813994414` passed; R3-B run `44814448975`
  passed; R3-C run `44815546348` passed; R3-D executable revision run
  [`44816105554`](https://github.com/Arthur-Levi/Levi-Intelligence/actions/runs/44816105554)
  **passed** on executable SHA `349bad9889dc818d04accd54b8d7d918b68228fd`.
  Its PostgreSQL 16 job verified the isolated test database identity, Alembic
  head and crawler lease columns, then reported **47 passed in 7.19 s**. Its
  backend job reported **222 passed, 1 warning in 2.26 s** and Ruff passed;
  the frontend build passed. All three jobs completed successfully.
- On this resumed Codespace, Ruff and Mypy passed. A local regression attempt
  with Python 3.14.2 reached `tests/test_health.py::test_liveness_returns_ok`
  after the earlier tests passed and then timed out at 40 seconds: **INCONCLUSIVE**
  for that local interpreter. The CI suite ran to completion on Python 3.12.16,
  the runtime configured in the workflow. PostgreSQL integration was executed only in
  the isolated CI service required by `backend/tests/integration/README.md`.

**Gate decision: PASS within the tested synthetic scope.** The completed CI
run verifies backend regression, PostgreSQL 16 integration and frontend build
for the R3-D executable revision. The local Python 3.14.2 timeout remains an
**INCONCLUSIVE** check; it does not change the verified
Python 3.12 CI result. The recommended next gate is SEC/OPS Gate 1B.2, carrying
forward the tenant/RLS and arbitrary external-effect limits above.
