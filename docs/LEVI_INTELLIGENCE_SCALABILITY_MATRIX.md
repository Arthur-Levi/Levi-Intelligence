# Synthetic PostgreSQL scalability matrix

## Required matrix

Run 1,000 directly inserted synthetic jobs with 1, 2, 4 and 8 workers, three independent repetitions each, on the **same exact commit SHA**. Each workflow execution provisions PostgreSQL 16 with a 512 MiB tmpfs, applies the same migrations, runs the identity preflight, and limits the benchmark to 600 seconds, 512 MiB process RSS and 12 observed database connections. The workflow remains capped at 15 minutes. Each run has its own JSON artifact; failed runs must be retained and repeated only as additional runs, never silently replaced.

The existing `Crawler synthetic capacity` workflow is extended with `repetition` and `revision` inputs. Dispatch it on the existing feature branch with `size=1000`, each `workers` value, `repetition=1,2,3`, and the exact pushed feature-branch SHA as `revision`. Because GitHub registers `workflow_dispatch` from the default branch, a newly added workflow or dispatch input may not be available until the default branch contains it. The existing workflow still accepts the original size and workers inputs. If the new inputs are unavailable, run the same 12 bounded commands in isolated disposable containers and retain each output with its commit SHA.

Download all 12 `capacity-report.json` artifacts under unique filenames, then from `backend/` run:

```sh
python -m scripts.crawler_capacity_matrix --output capacity-matrix.json reports/*.json
```

The aggregator rejects missing configurations, non-1,000-job reports and mixed commits. Its JSON contains all individual reports, including failures and aborts. For each worker count it summarizes successful runs with median, min, max and range for throughput, claim p95/p99 and completion p95/p99, while separately counting failed or aborted runs. A range is an observed run-to-run spread, not a confidence interval.

## Measured matrix (commit `8ff241d765d7144e1c5ec3d7ca1c4b66237f3e67`)

All 12 local runs completed successfully on one disposable PostgreSQL 16 instance, with each fixture cleaned between runs. The runner had 2 CPUs, a 768 MiB Python-container limit and a 512 MiB database tmpfs. Each run had a 60-second script limit. This is identical logical initialization and fixture strategy, but the sequential runs reused a PostgreSQL process and its caches; independent CI containers could differ. [Individual JSON reports](../backend/capacity-r21/) and [the aggregate with all runs](../backend/capacity-r21/matrix.json) preserve the evidence.

| Workers | Throughput median [min–max] jobs/s | Claim p95 / p99 median ms | Completion p95 / p99 median ms | Advisory / other wait samples per run | Peak connections | Peak RSS KiB | Outcome |
|---:|---:|---:|---:|---|---|---|---|
| 1 | 73.677 [73.213–74.008] | 10.327 / 15.535 | 8.530 / 11.885 | 0 / 0 each | 2 | 84,684–85,128 | 3/3 PASS |
| 2 | 82.586 [79.358–82.805] | 18.266 / 27.520 | 16.212 / 23.484 | 0 / 0 each | 3–4 | 86,096–86,396 | 3/3 PASS |
| 4 | 81.385 [81.342–81.818] | 46.037 / 69.715 | 24.803 / 45.831 | 89–95 / 0–1 | 5–6 | 86,576–86,928 | 3/3 PASS |
| 8 | 64.013 [54.814–79.368] | 147.260 / 187.043 | 36.368 / 52.939 | 518–694 / 1–2 | 9–10 | 87,580–88,068 | 3/3 PASS |

Claim p99 varied by 1.544, 5.795, 5.995 and 106.592 ms across the three runs at 1, 2, 4 and 8 workers respectively. Completion p99 varied by 0.705, 2.128, 3.793 and 22.366 ms. Throughput ranges were 0.795, 3.447, 0.476 and 24.554 jobs/s. These are observed ranges from three runs, not uncertainty bounds. The highest simultaneous waiting-session counts were 0, 0, 3 and 7 respectively. Sampling observed 2 active jobs at most for all configurations and 2 per tenant at most; the completion-only workload kept these counts low. Worker Jain fairness indices were at least 0.999968. Every run stayed below 12 observed connections, 512 MiB process RSS and 60 seconds. Process user CPU was 9.002–11.520 seconds per run, measured with `resource.getrusage`; PostgreSQL CPU was not measured.

**Provisional configuration: 2 workers for synthetic PostgreSQL lifecycle processing only.** Relative to 1 worker it raised median throughput about 12.1% while keeping claim p99 below 31 ms in every repetition. Four workers offered no median throughput gain and increased advisory waits and claim tail latency. Eight workers were less reliable in throughput and had much more advisory waiting. This recommendation is not production crawling capacity or an external-fetch worker setting.

The acceptance decision remains **INCONCLUSIVE** pending a green feature-branch CI result and database-side query/transaction timing evidence. The matrix itself is complete; this decision does not erase its successful runs. The earlier single-run observations are preserved in [the contention report](SEC_OPS_GATE_1B1_R21_POSTGRES_CONTENTION.md).

## Integrity interpretation

Each report checks the terminal completed state, one source record per job, one completed attempt generation, cleared final lease token and expiry, matching source/attempt tenant, sampled active bounds, sampled connection ceiling, process RSS and elapsed time. The fixture uses `example.test` strings and calls only repository `claim_next()` and `persist_result()`; the execution path has no website fetch. Admission, stale-write rejection and failure recovery are **NOT TESTED** by this workload. Active jobs and connections are snapshots; integration tests and code contracts support their bounds between samples, but the benchmark alone cannot prove an unsampled instantaneous maximum.
