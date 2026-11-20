# Synthetic PostgreSQL scalability matrix

## Required matrix

Run 1,000 directly inserted synthetic jobs with 1, 2, 4 and 8 workers, three independent repetitions each, on the **same exact commit SHA**. Each workflow execution provisions PostgreSQL 16 with a 512 MiB tmpfs, applies the same migrations, runs the identity preflight, and limits the benchmark to 600 seconds, 512 MiB process RSS and 12 observed database connections. The workflow remains capped at 15 minutes. Each run has its own JSON artifact; failed runs must be retained and repeated only as additional runs, never silently replaced.

The existing `Crawler synthetic capacity` workflow is extended with `repetition` and `revision` inputs. Dispatch it on the existing feature branch with `size=1000`, each `workers` value, `repetition=1,2,3`, and the exact pushed feature-branch SHA as `revision`. Because GitHub registers `workflow_dispatch` from the default branch, a newly added workflow or dispatch input may not be available until the default branch contains it. The existing workflow still accepts the original size and workers inputs. If the new inputs are unavailable, run the same 12 bounded commands in isolated disposable containers and retain each output with its commit SHA.

Download all 12 `capacity-report.json` artifacts under unique filenames, then from `backend/` run:

```sh
python -m scripts.crawler_capacity_matrix --output capacity-matrix.json reports/*.json
```

The aggregator rejects missing configurations, non-1,000-job reports and mixed commits. Its JSON contains all individual reports, including failures and aborts. For each worker count it summarizes successful runs with median, min, max and range for throughput, claim p95/p99 and completion p95/p99, while separately counting failed or aborted runs. A range is an observed run-to-run spread, not a confidence interval.

## Current evidence and recommendation

The previous single-run observations are preserved in [the contention report](SEC_OPS_GATE_1B1_R21_POSTGRES_CONTENTION.md). The required repeated matrix is **NOT MEASURED** until all 12 reports are attached. Initial configuration decision: **INCONCLUSIVE**. For a controlled synthetic trial before matrix completion, 2 workers is the conservative provisional starting point: its supplied 1,000-job run had 108.845 jobs/s, 12.661 ms claim p99 and zero sampled waits, while 4 and 8 workers showed higher throughput with materially higher claim p99. This does **not** establish a production crawler setting or a statistically safe optimum.

## Integrity interpretation

Each report checks the terminal completed state, one source record per job, one completed attempt generation, cleared final lease token and expiry, matching source/attempt tenant, sampled active bounds, sampled connection ceiling, process RSS and elapsed time. The fixture uses `example.test` strings and calls only repository `claim_next()` and `persist_result()`; the execution path has no website fetch. Admission, stale-write rejection and failure recovery are **NOT TESTED** by this workload. Active jobs and connections are snapshots; integration tests and code contracts support their bounds between samples, but the benchmark alone cannot prove an unsampled instantaneous maximum.
