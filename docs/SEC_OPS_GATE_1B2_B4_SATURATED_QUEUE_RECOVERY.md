# SEC/OPS Gate 1B.2-B.4 — saturated queue recovery

## Decision and scope

**Decision: PASS for the tested B.4 code and supported PostgreSQL 16 and disposable worker-container environment; final acceptance requires the final documentation commit's CI to succeed.** No production deployment approval or B.5 work is implied. The B.3 final commit `3a086e8d9e41d406dbdd9231b3570d83f715ec8f` was published; CI run `44944181581` succeeded. The B.4 code checkpoint `78f20d74b98b8481eeed737f5a5dbe3ef49e22ba` passed CI run `44947380317`. The D fixture commit `32e760521489ff0f0414809f17d8008b0a48e17c` passed CI run `44947606041`. The final pushed head must also be checked before unconditional acceptance.

The untracked `frontend/tsconfig.tsbuildinfo` is preserved. No schema migration, new public status, limit increase, worker framework, or child-process change was made.

## Defect and reproduction

At B.3, `claim_next()` held advisory transaction lock `681204117` but moved every selected expired `RUNNING` row directly to `QUEUED` without checking queue capacity. The new PostgreSQL 16 regression claimed one job, filled its tenant to 100 queued jobs, expired the lease, and invoked recovery through a worker claiming another tenant. **Before the fix, the test failed with 101 queued jobs (`assert 101 == 100`).** The test is now green. Initial admission already used the same advisory lock and checked queued capacity; expired retry was the bypass. Completion, failure, cancellation, and claim only reduce or preserve queued count.

## State and capacity contract

| Durable row state | Valid owner | Counts as queued | Counts as active | Counts as outstanding | Public polling status |
| --- | --- | --- | --- | --- | --- |
| `QUEUED` | No | Yes | No | Yes | `queued` |
| `RUNNING`, unexpired lease and token | Yes | No | Yes | Yes | `running` |
| `RUNNING`, expired lease, token cleared | No; recovery pending | No | No | Yes | `running` |
| `COMPLETED`, `FAILED`, `CANCELLED` | No | No | No | No | Matching terminal status |

The pending representation uses existing columns and survives PostgreSQL and worker restarts. Its expired timestamp is retained for selection; its token is cleared before any new generation. Its `next_attempt_at` holds the exponential retry deadline and, while blocked by capacity, a one-second scan deferral. Only a `QUEUED` transition consumes queued capacity. Admission now also caps all nonterminal work (`QUEUED` plus all `RUNNING`, including pending) at **10,008 global** and **102 per tenant**. These are finite outstanding-work bounds derived from the unchanged 10,000/100 queue caps plus 8/2 active caps. Admission is the only operation that increases outstanding work, and its counts and insertion share the advisory transaction lock. Existing pre-B.4 excess work, if present, is retained and new admission is refused until it drains. No accepted job is discarded.

The public status remains `running` during pending recovery because the existing polling schema has no pending value. This can overstate current execution to a polling client. The authoritative active count requires an unexpired lease, and stale token, generation, tenant, and company checks reject writes. Cancellation remains available through the existing endpoint. A new status would be an API and enum migration and was not necessary to enforce safety.

## Transaction, selection, and fairness

Admission takes lock `681204117`, rechecks idempotency, counts queued and `RUNNING` rows, applies both queue and outstanding caps, then inserts and commits. Recovery takes the same lock, selects at most **32** expired rows with `FOR UPDATE SKIP LOCKED`, closes an open attempt entry once, clears its token, and applies cancellation or retry exhaustion without needing queue capacity. For a retry, it counts queued rows in the same transaction and increments local counts for every transition, so several recovered rows cannot collectively exceed either cap. The new claim is selected only after these transitions and after valid active leases are counted; generation increments and the new attempt row are committed with the lease. Every repository operation retains B.3's finite checkout, connection, lock, statement, and whole-transaction budgets. No nested repository session or unbounded drain loop was introduced.

Selection orders expired work by `next_attempt_at`, creation time, and ID. A capacity-blocked row is deferred for at least one second, letting later tenants reach the next 32-row scan. A PostgreSQL test placed 33 older pending rows behind one saturated tenant and confirmed an eligible other tenant advanced on the second scan. Admission reserves only the last global slot for an expired job whose tenant has room, and only the last tenant slot for that tenant's expired job. Sustained competing admissions therefore cannot take every newly available slot. An unrelated tenant was admitted at 9,999 queued while the pending tenant remained full. This liveness argument assumes workers continue polling, the database eventually responds, retry deadlines elapse, and queue capacity eventually opens. The 64-row queued candidate window had a separate demonstrated starvation defect: 65 older jobs from an active-saturated tenant hid an eligible other tenant. Claim now excludes tenants already holding two valid leases before applying the 64-row bound.

No valid owner is created for pending rows. Heartbeat and result/failure persistence require an unexpired matching token. The attempt ledger's unique generation constraint, row lock, and five-attempt check prevent double ownership or retry-budget bypass. The 90-second renewable lease, global/tenant valid active caps **8/2**, maximum **five** attempts, and independent child deadline are unchanged.

## SQ-01 through SQ-24 evidence

All PostgreSQL references below are **new B.4 execution** against the disposable PostgreSQL 16 identity, even where a pre-existing regression supplied the assertion. “Container” is a manually executed actual worker entry point under the Compose restrictions; it is not a CI job.

| Item | Result and direct evidence |
| --- | --- |
| SQ-01 | PASS — pre-fix B.3 regression failed at 101 tenant queued; same test passes after fix. |
| SQ-02 | PASS — exact 10,000 global queued PostgreSQL case remained at or below 10,000 through saturation, admission, release, and recovery. |
| SQ-03 | PASS — exact 100 tenant queued case remained at or below 100; four concurrent recovery workers plus 12 admissions did not exceed it. |
| SQ-04 | PASS — one test saturated both limits simultaneously; pending stayed unowned until both had room. |
| SQ-05 | PASS — both counts and every recovery transition commit under advisory lock `681204117`; local counters cover multiple rows in one transaction. |
| SQ-06 | PASS — the 12-admission/four-worker PostgreSQL race reserved the final tenant slot for recovery. |
| SQ-07 | PASS — concurrent worker scans left one closed expired ledger entry and one queued transition. |
| SQ-08 | PASS — newly rerun `test_claim_races_and_repeated_disputes` and `test_competing_recovery_workers_single_current_attempt`; unique generation and token fencing remained intact. |
| SQ-09 | PASS — pending row and closed ledger survived actual worker SIGKILL and a stop/start of volume-backed PostgreSQL 16. |
| SQ-10 | PASS — after a slot opened, PostgreSQL test requeued pending work; actual restarted worker advanced to generation 2. |
| SQ-11 | PASS within stated scheduling assumptions — 32-row rotation, 64-candidate exclusion, last-slot reservation, and sustained admission race all passed. |
| SQ-12 | PASS — B.4 test counted zero valid active leases while an expired `RUNNING` pending row remained durable. |
| SQ-13 | PASS — newly rerun global active-cap PostgreSQL regression held at eight valid leases. |
| SQ-14 | PASS — newly rerun tenant active-cap and two-worker pool regressions held at two valid leases per tenant. |
| SQ-15 | PASS — B.4 test observed generations 1 and 2 with the first expired entry closed once and the second running; actual container later showed `[1, 2]` with `expired`, `failed`. |
| SQ-16 | PASS — newly rerun five-generation ledger regression ended at retry exhaustion with exactly five rows. |
| SQ-17 | PASS — B.4 cancellation during pending recovery became terminal with queue still full; newly rerun cancellation races rejected stale persistence. |
| SQ-18 | PASS — B.4 full-queue retry-exhaustion case became `FAILED` without a queue slot; five-generation regression confirms ceiling. |
| SQ-19 | PASS — B.4 test rejected stale result and failure after generation 2 claim; tenant and company fencing regressions reran. |
| SQ-20 | PASS — pending state survived volume-backed PostgreSQL stop/start; a separate `docker pause`/`unpause` outage left the actual worker alive and recovery resumed. The first tmpfs restart probe discarded its database by design and is excluded from durability evidence. |
| SQ-21 | PASS — actual worker SIGTERM preserved pending state; SIGKILL plus restart preserved and advanced it. |
| SQ-22 | PASS — 32 recovery and 64 claim row bounds; B.3 pool checkout, advisory/row lock, transaction-timeout, connection-loss, and two-worker tests reran successfully. |
| SQ-23 | PASS — actual restricted worker container kept 100 queued jobs at saturation, cleared the expired token, closed the ledger, and advanced after slot release/restart. |
| SQ-24 | PASS — 71 PostgreSQL integration tests and 273 unit/API/security/deadline/shutdown tests passed on B.4 code; Ruff, affected-module Mypy, Compose validation, and diff checks passed. Final commit CI remains a final gate. |

## Commands, environment, and measurements

The PostgreSQL tests used the CI-equivalent `postgres:16` image, exact isolated `levi_gate_1b1_ci` database/user identity, `scripts/postgres_ci.py` preflight and migrations, and synthetic tenant/company/job rows. The real global boundary seeded 99 synthetic tenants at 100 jobs plus one 100-job tenant. No production or development database was used. Reproduction and validation commands, with credentials supplied through `TEST_DATABASE_URL` rather than printed, were:

```bash
cd backend
python scripts/postgres_ci.py preflight
python scripts/postgres_ci.py migrate-and-verify
pytest -q -s tests/integration/test_crawler_saturated_recovery_b4.py
pytest -q tests/integration
pytest -q --ignore=tests/integration
ruff check app tests
mypy app/crawler_jobs/postgres.py app/api/v1/endpoints/prospecting.py
cd ..
CRAWLER_DATABASE_URL=postgresql+asyncpg://synthetic:synthetic@postgres:5432/synthetic docker compose config --quiet
git diff --check
```

The actual worker used `python -m app.workers.crawler`, UID/GID 10001, read-only root, no added capabilities, `no-new-privileges`, 512 MiB memory and swap ceiling, one CPU, 64 PIDs, and 1,024 file descriptors. A point-in-time sample was **56.14 MiB and two PIDs**. With 100 queued jobs and one expired attempt it showed `running`, 100 queued, token cleared, one closed `expired` ledger row. After one slot was freed and the worker restarted, it showed `failed`, 99 queued, two generations, and outcomes `expired` then `failed`; the synthetic `example.test` target caused the second attempt's terminal failure. The row made progress and never exceeded capacity.

At the real 10,000 boundary, observed single-run timings were: global queued count **0.004 s**, unrelated admission at 9,999 queued **0.034 s**, correlated last-slot reservation **0.013 s**, and recovery with 9,999 queued **0.020 s**. These are point measurements on disposable hardware, not production latency guarantees. The focused suite ran **9 tests in 8.79 s**; the full PostgreSQL suite ran **71 tests in 33.25 s**; the non-integration suite ran **273 tests in 13.17 s** with one upstream Starlette deprecation warning.

## Residual risks and rollback

Polling continues to show `running` for a durable recovery-pending row even though it has no valid active lease. Capacity and ownership decisions use the database lease fields, not the public status string. External HTTP execution remains at least once after a worker crash. The correlated reservation query and counts were measured at 10,000 synthetic jobs, not at production history or tenant cardinality. The manual container probe is not part of GitHub Actions.

No migration is required for rollback. Preserve the database and attempt ledger, stop crawler workers and pause new admissions, then revert B.4 commits through ordinary forward commits if needed; do not reset or force-push. **Do not restart B.3 workers against a saturated queue with expired jobs:** their recovery path can again overflow capacity. Drain pending work with this fixed version or first establish headroom for every pending tenant and globally before any earlier worker version resumes. Confirm token fencing and counts after rollback. B.5 requires separate approval.
