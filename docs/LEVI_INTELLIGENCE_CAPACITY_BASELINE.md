# Levi Intelligence crawler capacity baseline (R2)

## Configured boundaries

The current code admits at most **10,000 pending jobs globally** and **100 pending jobs per tenant**. Claims allow **8 active globally** and **2 active per tenant**. A worker process accepts 1–8 local slots. Five attempts, a 32-row recovery scan and a 64-row claim scan are configured. These are admission and execution ceilings, not measured throughput.

## Measured capacity

No R2 benchmark has executed yet. Tested worker counts, throughput, latency, CPU, memory, PostgreSQL connections, lock waits, first bottleneck, saturation behavior and post-saturation recovery are **NOT MEASURED**. The prior 13 PostgreSQL integration tests prove selected correctness properties, not sustainable capacity. No maximum safe production capacity is claimed.

| Queue level | Workers | Completion throughput | Claim p95 | CPU / peak memory | Decision |
|---:|---|---|---|---|---|
| 100 | 1, 2, 4, 8 available manually | NOT MEASURED | NOT MEASURED | NOT MEASURED | NOT MEASURED |
| 500 | 1, 2, 4, 8 available manually | NOT MEASURED | NOT MEASURED | NOT MEASURED | NOT MEASURED |
| 1,000 | 1, 2, 4, 8 available manually | NOT MEASURED | NOT MEASURED | NOT MEASURED | NOT MEASURED |
| 2,500 | requires isolated runner | NOT MEASURED | NOT MEASURED | NOT MEASURED | UNVERIFIED |
| 5,000 | requires isolated runner | NOT MEASURED | NOT MEASURED | NOT MEASURED | UNVERIFIED |
| 10,000 | requires isolated runner and 100 tenants | NOT MEASURED | NOT MEASURED | NOT MEASURED | UNVERIFIED |

The manual `Crawler synthetic capacity` workflow uses a disposable PostgreSQL 16 service, synthetic tenants, a 512 MiB PostgreSQL tmpfs, 100/500/1,000 job choices, and 1/2/4/8 local worker choices. It records claim and completion latency percentiles, throughput, elapsed time, memory high-water mark, sampled connection count and lock waits. It directly inserts fixture jobs; admission API throughput and 10,000-job saturation are **NOT MEASURED** by this script. The script has a 600-second maximum input, 512 MiB process memory abort condition, 12-connection abort condition, 15-minute workflow timeout and cleanup in `finally`. It does not contact any website.

The 2,500/5,000/10,000 queue levels require a separately isolated runner with enough disposable PostgreSQL memory and disk, a monitored connection ceiling, and an approved time and performance SLO. The 10,000 level requires at least 100 synthetic tenants because each tenant has a 100-pending limit. Its test must exercise real admission counts and distinguish fixture insert time from API admission time. A conservative proposed SLO for review is p95 claim latency under 1 second, zero duplicate committed results, zero stale writes, and complete recovery within 2 minutes after lease expiry plus backoff. This is a proposal, not an accepted threshold.

## Likely bottlenecks to measure

Admissions and claims share one PostgreSQL advisory transaction lock, which serializes both paths across processes. Claims scan 64 queued candidates and count active jobs per candidate. These are architectural hypotheses, not an observed first bottleneck. Measure lock wait time and database CPU before changing either control. Do not raise active limits merely to increase benchmark numbers.
