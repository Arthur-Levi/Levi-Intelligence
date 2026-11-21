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

## Implementation, measurements and final decision

Pending checkpoints B–E. No gate decision is made from the baseline alone.
