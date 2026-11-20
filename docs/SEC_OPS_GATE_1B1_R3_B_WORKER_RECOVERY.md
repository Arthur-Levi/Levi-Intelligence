# Gate 1B.1-R3-B: worker crash, fencing and recovery

## Execution

Branch `feature/build-006a-secure-crawler`; R3-A base commit
`de9899f6b431fd740b8d59c6c6f357b8b64e6a8c`. Tests used the disposable
PostgreSQL 16 database and migrations described in the R3-A report. The Python
test container was limited to 512 MiB and two CPUs; the database used a 512 MiB
tmpfs. Each scenario has a 5–10 second operation timeout and the suite has a
120-second outer timeout. At most one child worker and four competing recovery
tasks ran in a case. Fixtures clean up in `finally`.

`crawler_crash_child.py` claims a synthetic `example.test` job and prints only
phase, job ID and attempt ID. The parent waits for a committed claim marker, or
for a synthetic crawler's processing marker, then sends `SIGKILL` and awaits
the process exit. It does not make an HTTP request. A normal Python exception
is not used as evidence of process death.

## Scenario evidence

| Scenario | Test | Durable timeline and result |
|---|---|---|
| B1 after claim | `test_killed_worker_recovered_after_lease_expiry[claimed]` | Claim 1 committed; child killed; running job remains; controlled expiry; retry queued; claim 2 completes; one source |
| B2 during processing | `test_killed_worker_recovered_after_lease_expiry[processing]` | Same timeline after synthetic crawler entered processing; attempt 1 is `expired`, attempt 2 `completed` |
| B3 stale return | `test_stale_worker_rejected_after_reclaim` | Old heartbeat false; old result and failure rejected; new token completes; generation 2 intact |
| B4 competing recovery | `test_competing_recovery_workers_single_current_attempt` | Two expired jobs, four contenders, two distinct current claims and exactly one open attempt per job |
| B5 exhaustion | `test_retry_exhaustion_has_five_ledger_rows` | Generations 1–4 expire; generation 5 ends `retry_exhausted`; job is terminal failed; no sixth claim |
| B6 cancellation | `test_cancellation_race_rejects_lease_and_result`, `test_queued_cancellation_races_claim_without_result` | Cancellation races heartbeat and claim; final cancelled state, revoked lease, no source, attempt history matches winner |

All seven tests use the production PostgreSQL repository, independent sessions,
and fresh authoritative reads for job, attempt and source assertions. Recovery
uses a controlled timestamp update and the real retry scheduling code rather
than a 90-second wall-clock wait. The attempt count and source count assertions
cover no double completion, no duplicate committed result, no silent job loss,
and no incorrect attempt generation in these scenarios. Existing integration
tests cover tenant predicates and active limits; this suite does not replace
their broader evidence. Direct privileged SQL can bypass application limits.

## Results and limits

- `timeout 120 pytest -q tests/integration/test_crawler_worker_recovery_r3.py`:
  **7 passed in 3.50 s**.
- `ruff check app tests`: passed.
- `mypy tests/integration/crawler_crash_child.py tests/integration/test_crawler_worker_recovery_r3.py --follow-imports=silent`:
  passed, no issues in two files.

This is **PASS for R3-B's tested synthetic PostgreSQL recovery scope**.
External effects remain at least once if a process dies after an arbitrary
effect and before its database result commit. No exactly-once claim is made.
Database interruption and concurrent admission remain for R3-C.
