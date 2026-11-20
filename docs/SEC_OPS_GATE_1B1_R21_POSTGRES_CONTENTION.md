# Gate 1B.1-R2.1: PostgreSQL contention investigation

## Scope and decision

This gate measures synthetic PostgreSQL lifecycle processing. It does not measure website fetching, admission throughput, production load, or sustained crawling capacity. The 12-run matrix is complete at commit `8ff241d765d7144e1c5ec3d7ca1c4b66237f3e67`. Feature-branch CI run `44768052992` passed backend, postgres-integration and frontend. The directed timing evidence below closes the lock-versus-query question for this synthetic completion workload.

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

The 64-candidate query filters cancel state, retry time and attempts, then orders by creation time and tenant. The recovery query has status and lease-expiry predicates but no matching composite index. Candidate scanning can perform one tenant count per examined candidate. These costs lengthen the advisory-lock hold. The original benchmark samples wait states but does not time lock hold or SQL; the directed probes below add timing and plans.

`pg_locks` sampling now classifies a waiting session with an ungranted advisory lock as advisory, and an ungranted non-advisory lock as other. `pg_stat_activity.wait_event_type` and `wait_event` are reported as bounded counts of session snapshots. A row lock can surface as a transaction-id wait, so “other” is not proof of a particular row. Sampling at 100 ms misses short waits and can observe the same wait repeatedly. The sample query itself adds load. Peak connections and active counts are sampled, not continuous maxima. Process CPU uses the `resource.getrusage(RUSAGE_SELF)` user/system delta and excludes PostgreSQL CPU. Process memory is its high-water RSS on Linux.

## Contention assessment

The global advisory lock is a **structural serialization point** for claims. The supplied earlier 8-worker run shows higher claim p99 and nonzero sampled lock waits than the 2-worker run, but cannot establish that the 158 samples were advisory waits. In the new same-revision matrix, 4-worker runs recorded 89–95 advisory waiting-session samples and 0–1 other-lock samples; 8-worker runs recorded 518–694 advisory and 1–2 other samples. The maximum simultaneous waiting-session counts were 3 and 7 respectively. At 1 and 2 workers, no lock waits were sampled. The 8-worker median claim p99 was 187.043 ms versus 27.520 ms at 2 workers, while median throughput fell from 82.586 to 64.013 jobs/s. These observations support advisory-lock serialization as a material contributor under this workload. They do not measure lock-hold time, demonstrate that it is the only bottleneck, or exclude runner variability, Python scheduling and query cost. Row/transaction lock waits were rare in the samples, but short waits may have been missed. Connection peaks of 9–10 at 8 workers stayed below the 12-connection ceiling, so connection exhaustion was not observed.

Any later lock change must preserve atomic global and tenant active counts, row selection, attempt generation, lease ownership, fencing against stale writes, retry limits and rollback behavior across processes. Gate R2.1 makes no lock or lifecycle changes.

## Directed PostgreSQL 16 timing probes

One additional 1,000-job run each at 2, 4 and 8 workers used a disposable PostgreSQL 16 container with a 512 MiB tmpfs, a 2-CPU Python runner and the same fixture cleanup and identity preflight as the matrix. These are directed observations, not extra matrix repetitions. The [individual probe JSON files](../backend/capacity-r21/) contain the plans, bounded telemetry and integrity checks. Timings use SQLAlchemy cursor execution wall time. The claim transaction runs from the engine `begin` event to the `commit` request; lock hold runs from completion of `pg_advisory_xact_lock` to that request. They exclude the commit round trip and pool checkout. Advisory acquisition includes server wait, client round trip and scheduling; it is not a pure server wait measurement.

| Workers | Throughput jobs/s | Claim p99 ms | Advisory acquire p50 / p99 ms | Lock hold p50 / p99 ms | Claim transaction p50 / p99 ms | Advisory wait samples |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | 70.447 | 36.144 | 1.104 / 6.224 | 8.795 / 22.929 | 10.051 / 29.469 | 2 |
| 4 | 70.219 | 74.118 | 13.402 / 43.364 | 11.612 / 29.273 | 25.510 / 65.465 | 106 |
| 8 | 66.031 | 169.623 | 69.404 / 142.176 | 12.408 / 30.980 | 82.183 / 158.765 | 614 |

Across the runs, advisory acquisition medians rose much faster than lock-hold medians. At 8 workers, the 69.404 ms median acquisition accounted for most of the 82.183 ms median pre-commit transaction duration, though these separately calculated medians must not be subtracted as paired samples. This directly supports serialization at the advisory lock as the main cause of the added claim latency in this workload. It does not prove it is the only cost. Query and client work inside the critical section lengthen every competing worker's wait.

| Workers | Recovery p50 ms | Selection p50 ms | Global count p50 ms | Tenant count p50 ms | Tenant count calls | Claim lock-hold total s |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | 0.823 | 1.676 | 0.685 | 0.679 | 1,000 | 9.704 |
| 4 | 1.456 | 1.777 | 0.830 | 0.817 | 1,607 | 12.719 |
| 8 | 1.590 | 1.780 | 0.856 | 0.887 | 1,650 | 13.573 |

The per-cursor SQL timings include driver and network round trips, so they are upper bounds on server execution rather than pure SQL time. The cursor timing totals for recovery, selection, global and tenant counts, attempt insert and job update were 5.955, 8.466 and 8.974 seconds at 2, 4 and 8 workers, respectively. The corresponding pre-commit lock-hold totals were 9.704, 12.719 and 13.573 seconds. The residual includes ORM materialization, Python work, result fetching, driver round trips and scheduling; these measurements cannot apportion that residual further. More candidate tenant counts at 4 and 8 workers are consistent with full tenant slots causing extra checks.

`EXPLAIN (ANALYZE, BUFFERS)` on the 1,000 queued-row fixture showed: recovery used `ix_crawler_jobs_claim`, 2 shared-hit buffers, 0.023 ms server execution; global active count used the same index, 2 shared-hit buffers, 0.015 ms; tenant active count used the same status index, 2 shared-hit buffers, 0.024 ms; selection used a sequential scan of 1,000 rows and a sort before locking 64 rows, 147 shared-hit buffers, 275 kB sort memory, 0.845 ms server execution. The selection plan had 0.096 ms planning time. At this fixture size, the planner favored a scan and sort over the `(status, created_at)` index because the query also orders by tenant and filters retry/cancel state. These plans explain measurable selection cost but do not explain the 8-worker acquisition rise by themselves. The fixture had no expired leases or active rows when plans were captured; recovery and running-count plans therefore do not establish costs under a populated running or expired set. No index change is justified by this evidence alone.

All three directed runs completed 1,000 jobs with one source and one completed attempt per job, no duplicate completion, matching tenant IDs and cleared lease state. Sampled active counts remained within 8 global and 2 per tenant. Peak connections were 3, 5 and 9; peak Python RSS was 86,156, 87,064 and 89,144 KiB; durations were 14.195, 14.241 and 15.144 seconds. All remained within 12 connections, 512 MiB RSS and 60 seconds. These are sampled connection and active maxima. Admission, stale writes and failure recovery are covered by integration tests, not by these completion-only probes.

**Provisional initial setting: 2 workers** for synthetic PostgreSQL lifecycle processing. It had the highest median throughput in the original matrix, almost no sampled advisory waits and the shortest claim tail in both the matrix and directed probes. Four workers did not improve throughput here, and eight worsened throughput and wait time. This setting is not a production crawler capacity claim.

**Gate decision: PASS for the scoped synthetic PostgreSQL lifecycle configuration.** The measured acquisition growth and stable lower lock-hold times explain the observed contention sufficiently to support the provisional 2-worker setting. The twelve matrix runs and three directed probes passed integrity and resource checks; the prior feature-branch CI passed all three jobs, and the disposable PostgreSQL integration suite passed 16 tests during this closure. This decision does not claim production capacity or that all client/server overhead has been attributed.

## Remaining limits and later work

1. Measure recovery with a populated expired-lease fixture and admission with concurrent claims before changing indexes or the algorithm.
2. Compare database-side CPU and pool checkout time before proposing an optimization.
3. Repeat on an independent runner with a fresh PostgreSQL process per run to quantify cache and runner effects.
