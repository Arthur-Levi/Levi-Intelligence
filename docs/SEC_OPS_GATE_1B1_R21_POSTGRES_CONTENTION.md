# Gate 1B.1-R2.1: PostgreSQL contention investigation

## Scope and decision

This gate measures synthetic PostgreSQL lifecycle processing. It does not measure website fetching, admission throughput, production load, or sustained crawling capacity. The 12-run matrix is complete at commit `8ff241d765d7144e1c5ec3d7ca1c4b66237f3e67`. Acceptance remains **INCONCLUSIVE** pending a green feature-branch CI result and server-side query/transaction timing evidence.

## Observed baseline (single runs)

| Jobs | Workers | Throughput (jobs/s) | Claim p99 (ms) | Lock wait samples |
|---:|---:|---:|---:|---:|
| 100 | 2 | 131.951 | 34.200 | 0 |
| 500 | 2 | 151.792 | 9.409 | 0 |
| 1,000 | 2 | 108.845 | 12.661 | 0 |
| 1,000 | 4 | 127.491 | 53.011 | 0 |
| 1,000 | 8 | 139.749 | 74.512 | 158 |

These are supplied single-run observations from the prior harness. That harness checked `pg_stat_activity` every 25 completions and summed the number of sessions with `wait_event_type = 'Lock'`. The 158 figure is **waiting-session snapshots**, not 158 distinct lock acquisitions or events. Its sampling interval varied with throughput; zero means no wait was observed at those instants. It did not distinguish advisory from row or transaction locks.

## Lock scope and transaction path

`create_company_enrichment_job()` acquires `pg_advisory_xact_lock(681204117)` after the initial idempotency lookup, then repeats the lookup, counts global and tenant queued jobs, inserts the job, updates the company, records the audit event, and commits. This serializes admission decisions across API processes while preserving duplicate-request idempotency and the 10,000 global / 100 tenant pending bounds.

`PostgresCrawlerJobRepository.claim_next()` takes the same transaction-scoped lock before recovering up to 32 expired leases. Each recovered row may require an attempt lookup and an update. It then counts active jobs globally, selects and locks up to 64 queued candidates, counts active jobs for each candidate until one has room, inserts an attempt, flushes, and commits. The lock remains held until the transaction ends on every return path. Consequently concurrent claims, and admissions racing with claims, serialize at this lock. The lock protects the global 8-active and tenant 2-active decisions as one atomic claim transaction. Row locks additionally protect selected jobs; completion and heartbeat use row locks and lease-token checks but do not acquire the global advisory lock.

The 64-candidate query uses the `(status, created_at)` index for queue order but filters cancel state, retry time, attempts and optional tenant after that. The recovery query has status and lease-expiry predicates but no matching composite index. Candidate scanning can perform one tenant count per examined candidate. These are potential query and round-trip costs, especially when the first queued tenants are full. They also lengthen the advisory-lock hold. An `EXPLAIN (ANALYZE, BUFFERS)` on a disposable populated fixture and measured lock-hold / transaction durations are required before ranking query cost against lock serialization. The current benchmark samples wait states but does not directly time server-side lock hold duration or query execution; claim wall latency includes pool checkout, lock wait, SQL, ORM and Python scheduling.

`pg_locks` sampling now classifies a waiting session with an ungranted advisory lock as advisory, and an ungranted non-advisory lock as other. `pg_stat_activity.wait_event_type` and `wait_event` are reported as bounded counts of session snapshots. A row lock can surface as a transaction-id wait, so “other” is not proof of a particular row. Sampling at 100 ms misses short waits and can observe the same wait repeatedly. The sample query itself adds load. Peak connections and active counts are sampled, not continuous maxima. Process CPU uses the `resource.getrusage(RUSAGE_SELF)` user/system delta and excludes PostgreSQL CPU. Process memory is its high-water RSS on Linux.

## Contention assessment

The global advisory lock is a **structural serialization point** for claims. The supplied earlier 8-worker run shows higher claim p99 and nonzero sampled lock waits than the 2-worker run, but cannot establish that the 158 samples were advisory waits. In the new same-revision matrix, 4-worker runs recorded 89–95 advisory waiting-session samples and 0–1 other-lock samples; 8-worker runs recorded 518–694 advisory and 1–2 other samples. The maximum simultaneous waiting-session counts were 3 and 7 respectively. At 1 and 2 workers, no lock waits were sampled. The 8-worker median claim p99 was 187.043 ms versus 27.520 ms at 2 workers, while median throughput fell from 82.586 to 64.013 jobs/s. These observations support advisory-lock serialization as a material contributor under this workload. They do not measure lock-hold time, demonstrate that it is the only bottleneck, or exclude runner variability, Python scheduling and query cost. Row/transaction lock waits were rare in the samples, but short waits may have been missed. Connection peaks of 9–10 at 8 workers stayed below the 12-connection ceiling, so connection exhaustion was not observed.

Any later lock change must preserve atomic global and tenant active counts, row selection, attempt generation, lease ownership, fencing against stale writes, retry limits and rollback behavior across processes. Gate R2.1 makes no lock or lifecycle changes.

## Gate 1B.1-R3 work

1. Capture sanitized `EXPLAIN (ANALYZE, BUFFERS)` for the claim and recovery queries with the 1,000-job fixture, plus transaction/lock-hold timings using disposable-only instrumentation.
2. Exercise concurrent admission, stale writes, lease expiry/retry recovery and failure paths in a separate bounded workload; this completion-only benchmark marks them NOT TESTED.
3. Compare database-side CPU and pool checkout time before proposing an index or claim algorithm change.
4. Repeat the matrix on an independent runner with a fresh PostgreSQL process per run to quantify cache and runner effects.
