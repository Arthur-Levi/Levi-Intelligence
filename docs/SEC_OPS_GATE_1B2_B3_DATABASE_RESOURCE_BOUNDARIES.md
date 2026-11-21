# SEC/OPS Gate 1B.2-B.3 — crawler database resource boundaries

## Checkpoint A: observed baseline (before B.3 changes)

Branch `feature/build-006a-secure-crawler`, accepted head `ea4ee0f`, CI run `44923408551` success. The pre-existing untracked `frontend/tsconfig.tsbuildinfo` was preserved. This checkpoint records the current contract and measured defaults; it makes no runtime change.

The crawler worker creates **one** SQLAlchemy async engine in `app.workers.crawler.run()` and one session factory over it. Its independently supervised crawler child receives no database URL or connection descriptor. Repository methods open one session each; `claim_next`, `heartbeat`, and fenced persistence use `session.begin()` transactions. Cancellation checks use a read-only session. Methods do not hold a repository session while calling another repository method. The worker disposes its engine on shutdown; the application waits up to five seconds for disposal, then exits so PostgreSQL can recover any still-owned leases. API and discovery processes use their own cached engine from `app.db`; the API engine is not shared with the crawler process.

| Boundary | Declared/current code | Observed effective baseline |
| --- | --- | --- |
| Pool implementation | `create_async_engine(..., pool_pre_ping=True, pool_size=concurrency)` | `AsyncAdaptedQueuePool` |
| Crawler concurrency/pool size | Compose concurrency 2; worker clamps to 1–8 | Size 2 at default |
| Overflow | Unset | 10, allowing **12 connections per crawler replica** |
| Pool checkout | Unset | 30 seconds |
| Connection establishment | No `connect_args` | asyncpg `connect(timeout=60)` default |
| Driver command timeout | Unset | `None` |
| Server statement/lock/idle-in-transaction timeouts | Unset | All **0 ms** (disabled) in disposable PostgreSQL 16 |
| Recycle/validation | `pool_pre_ping=True`; no recycle | Pre-ping true; recycle `-1` (disabled) |
| API/discovery engine | `app.db.get_engine()` default pool | Pool 5, overflow 10, checkout 30 seconds, pre-ping true |
| Transaction budget | Context-manager commit/rollback; no whole-transaction timer | No explicit end-to-end bound |

The disposable PostgreSQL 16 instance used tmpfs and trust authentication with synthetic identity. `pg_settings` showed `max_connections=100`, `superuser_reserved_connections=3`, `reserved_connections=0`, and `statement_timeout=lock_timeout=idle_in_transaction_session_timeout=0ms`. A baseline `pg_stat_activity` query saw one active inspection connection; this is not an idle pooled worker connection. The development Compose PostgreSQL image does not itself set `max_connections`, and production settings were not inspected.

For a shared database with these observed defaults, the **pre-change potential** is `12W + 15A + 15D + O`, where `W` is crawler worker replicas, `A` is API processes, `D` is discovery processes, and `O` covers any other pools or clients. PostgreSQL permits at most 97 non-superuser connections before its three superuser reserves; retaining an additional five ordinary connections for administration yields `12W + 15A + 15D + O <= 92` as a conservative deployment budget on this disposable configuration. One crawler, one API and one discovery process could demand 42 connections before other clients. Eight crawlers plus one API could demand 111 and exceed the observed server allowance. The global active-job cap of eight does not cap connections: a pool can open during claiming, cancellation, heartbeat and recovery even with few active jobs. Only the single-crawler development Compose topology and small disposable tests are currently evidenced; no production replica count is known.

The main weaknesses are ten implicit overflow connections per worker, long default checkout/connection waits, disabled server timeouts, no whole-transaction budget, and repository awaits that can delay cancellation or shutdown during database faults. `claim_next` still uses advisory transaction lock `681204117`; B.3 must bound lock waits without bypassing it. B.4 retains ownership of saturated-queue recovery. The 90-second renewable lease, active limits 8/2, maximum five attempts, ledger, tenant checks, fenced persistence, idempotent admission, HTTP 202 route, independent child deadline and B.2.1 container ceilings remain required.

## Implemented boundary (checkpoints B–D)

The worker still creates one engine and one session factory. `CrawlerDatabaseResources.from_env()` rejects non-finite, negative, out-of-range and inconsistent values before opening the engine. `CRAWLER_DATABASE_URL` remains mandatory. The independent crawler child still receives no database credentials or connections. Each supervisor engine uses `AsyncAdaptedQueuePool`, pre-ping, zero overflow and an application name containing a short worker identifier for `pg_stat_activity`. Pool size defaults to the unchanged concurrency of two. The one-session-per-operation repository pattern avoids nested pool checkout; heartbeat, cancellation checks, claim, recovery and persistence can contend for the two slots, with a finite checkout wait. Recycling remains disabled because pre-ping validates checkout and age-based replacement would add connection churn without a measured need.

| Setting (seconds unless noted) | Default | Allowed range | Enforcement |
| --- | ---: | ---: | --- |
| `CRAWLER_WORKER_CONCURRENCY` | 2 | 1–8 | Application tasks; default remains 2 |
| `CRAWLER_DB_POOL_SIZE` | 2 | concurrency–8 | SQLAlchemy pool |
| `CRAWLER_DB_MAX_OVERFLOW` | 0 | 0 only | SQLAlchemy pool |
| `CRAWLER_DB_POOL_TIMEOUT_SECONDS` | 2 | 0.1–5 | Checkout |
| `CRAWLER_DB_CONNECT_TIMEOUT_SECONDS` | 3 | 0.1–5 | asyncpg connection establishment |
| `CRAWLER_DB_LOCK_TIMEOUT_SECONDS` | 1 | 0.1–10 | PostgreSQL session setting |
| `CRAWLER_DB_STATEMENT_TIMEOUT_SECONDS` | 5 | 0.2–30 | PostgreSQL session setting |
| `CRAWLER_DB_IDLE_TRANSACTION_TIMEOUT_SECONDS` | 5 | 0.2–30 | PostgreSQL session setting |
| `CRAWLER_DB_COMMAND_TIMEOUT_SECONDS` | 6 | 0.2–30 | asyncpg command timeout |
| `CRAWLER_DB_TRANSACTION_TIMEOUT_SECONDS` | 15 | 1–60 | Python `asyncio.timeout`, including session cleanup |

Startup requires `lock < statement < command < transaction`, idle-in-transaction no greater than transaction, and pool size at least concurrency. The 1-second lock wait protects the existing advisory transaction lock `681204117` and row locks. A 5-second statement limit does not bound a multi-statement transaction; the 15-second application timer covers the whole repository operation, including pool checkout and commit or rollback. `asyncio.CancelledError` is propagated. SQLAlchemy failures and transaction timeouts become sanitized `CrawlerDatabaseError`; a failed or ambiguous commit is never retried as a proven rollback. The authoritative row and attempt ledger must be reconciled after recovery. Ownership and generation checks remain inside fenced persistence transactions.

Database failures in worker tasks trigger per-worker claim backoff of 1, 2, 4, 8 then at most 10 seconds; SIGTERM/SIGINT still stop admission immediately. The hard child deadline is enforced by the independent process supervisor even while a PostgreSQL heartbeat awaits a row lock or the server is paused. If cleanup cannot finish within the existing application shutdown budget, the worker exits and leaves the lease for fenced PostgreSQL recovery. The 90-second renewable lease, maximum five attempts, global/tenant active caps 8/2, queued caps 10000/100, tenant scoping, admission idempotency and HTTP 202 contract are unchanged. Successful external HTTP execution remains at-least-once possible.

## Connection budget and measured runtime

For worker replica count `W`, API process count `A`, discovery process count `D`, and all other connection demand `O`, the configured default ceiling is **`2W + 15A + 15D + O`**. On the disposable PostgreSQL 16 instance with `max_connections=100`, three superuser reserves and five additional administrative connections reserved by policy, deployment must satisfy **`2W + 15A + 15D + O <= 92`**. A larger configured worker pool substitutes its actual pool size for 2. For example, one worker, one API and one discovery process can demand 32 connections; 32 workers plus one API and one discovery process can demand 94 and exceed this measured budget. These are ceilings, not a production capacity recommendation. The supported tested topology is two disposable workers and one synthetic database; production PostgreSQL settings, other clients and replica counts remain unverified.

In disposable PostgreSQL 16, `pg_stat_activity` saw two sessions at the effective two-slot cap. A third checkout failed in approximately 0.2 seconds in the fast test profile, with two sessions still present. After engine disposal, the named sessions returned to zero. The server reported the test profile's effective `statement_timeout=200ms`, `lock_timeout=100ms`, and `idle_in_transaction_session_timeout=200ms`; a synthetic `pg_sleep(1)` and row/advisory lock contention failed within one second. Whole-transaction timeout and connection reuse after rollback also passed. A separate worker-default connection attempt against the paused disposable server raised `TimeoutError` after **3.005 seconds**. The application name is for measurement, not an authorization boundary.

In the actual disposable worker-container test, two workers used the real `app.workers.crawler` entry point and synthetic crawling service. `pg_stat_activity` showed a maximum of three aggregate worker sessions at sampling time: two for one worker and one for the other. Observed memory was 65.46 MiB and 47.32 MiB, with two PIDs each at that sample; this was a point-in-time sample, not a peak resource profile. Both retained UID/GID 10001, read-only root, dropped capabilities, no-new-privileges, 512 MiB memory with no extra swap, one CPU, 64 PIDs and 1024 file descriptors. After SIGTERM, named worker sessions returned to zero. With PostgreSQL paused during a blocked synthetic child, the child's PID disappeared by its 30-second execution deadline while the worker parent remained alive. Stopping the parent left the attempt `running`, one ledger row and zero sources. After unpausing PostgreSQL and expiring the lease, the row returned to `queued`, the ledger outcome became `expired`, and the source count remained zero. The earlier database-stop probe reset its tmpfs database and was not used as durable recovery evidence.

## DB-01–DB-24 evidence

All test inputs were synthetic. Process tests had external command timeouts; Docker fixtures were disposable. “PG” means a real disposable PostgreSQL 16 instance, “container” means the actual worker entry point, and “unit” means in-process test evidence.

| ID | Evidence and result |
| --- | --- |
| DB-01–03 | PG: effective size 2, overflow 0, two named sessions; third checkout failed within the 0.2-second test limit. PASS. |
| DB-04 | Unit: invalid and inconsistent settings rejected at startup. PASS. |
| DB-05 | Container-level network probe: paused PostgreSQL connection failed in 3.005 seconds under the 3-second default. PASS. |
| DB-06 | PG: interrupted connections and subsequent checkout/reuse tests. PASS. |
| DB-07–08 | PG: `pg_sleep` and row-lock waiter interrupted under configured test limits. PASS. |
| DB-09 | PG: contended advisory lock `681204117` failed within the test limit; lock retained. PASS. |
| DB-10–12 | PG: idle transaction killed, transaction timer fired, rollback and later pool reuse succeeded. PASS. |
| DB-13 | PG: existing ambiguous-commit reconciliation test avoids blind retry and duplicate result. PASS. |
| DB-14–15 | PG: interrupted heartbeat, cancellation read, result and failure persistence preserved durable state and fencing. PASS. |
| DB-16 | PG/process and container: child exited at deadline while heartbeat or PostgreSQL was blocked; parent remained responsive. PASS. |
| DB-17 | Unit: bounded claim backoff and shutdown during outage; no tight retry loop. PASS. |
| DB-18 | Unit plus container: active database wait was cancelled on shutdown; container stopped within its supervisor budget. PASS. |
| DB-19–20 | PG: stale result/failure rejected, retry exhaustion retained five ledger rows. PASS. |
| DB-21 | PG and container: named connections returned to zero after engine/worker shutdown. PASS. |
| DB-22 | PG: two independent size-two pools and tenant active cap 2; existing lifecycle regression covers global cap 8. PASS for tested topology. |
| DB-23 | Actual disposable worker containers: pool cap, named sessions, database pause, child deadline, shutdown and lease recovery observed. PASS. |
| DB-24 | 240 unit/security tests and 58 PG crawler integration tests passed; Compose, Ruff, affected-module Mypy and diff check passed. PASS. |

The focused Checkpoint D validation also ran 43 worker/database/process tests and 40 PG tests before the broader 240/58 runs. No R2.1 capacity matrix was repeated. The connection establishment measurement was an external bounded runtime probe rather than a committed automated test; its command had a 15-second outer timeout. Resource sampling and DB-22 do not establish production-scale capacity.

GitHub Actions CI run **44931038752** for implementation/test commit `07c02bf` completed successfully. Earlier B.3 checkpoint runs `44924163295` (A), `44929136552` (B) and `44929530983` (C) also completed successfully. The documentation-only final commit requires its own CI verification after push.

## Residual risks, rollback and decision

The independent child execution deadline does not bound PostgreSQL finalization after crawling; repository operations have a 15-second cooperative application budget plus server and driver limits. A blocked kernel task or unsupported OS can still rely on the existing supervisor/container hard-stop boundary. A database outage can leave a running lease until expiry; it must not be interpreted as a successful result or exactly-once HTTP execution. Ambiguous commit status requires authoritative reconciliation. API/discovery pool defaults were not altered. Saturated-queue recovery is the separate B.4 issue.

Rollback, if needed, is to redeploy the last accepted B.2.2-R1 worker image and remove the B.3 worker-specific environment overrides, then allow outstanding 90-second leases to expire and reconcile the attempt ledger. Do not revert schema history, bypass fencing or reassign live leases manually. The pre-B.3 image restores the weaker implicit pool/timeout defaults, so this is a recovery procedure, not an equivalent safety posture.

**Decision: PASS within the supported disposable PostgreSQL 16 and worker-container runtime.** This decision is contingent on the final pushed commit's GitHub Actions run completing successfully; no production deployment approval is implied. Entry to B.4 requires retaining these pool, timeout, child-deadline and fencing bounds, and a separate plan for the known saturated-queue recovery defect. B.4 work has not begun.
