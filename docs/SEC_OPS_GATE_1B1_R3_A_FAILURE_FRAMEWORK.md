# Gate 1B.1-R3-A: bounded failure injection framework

## Scope and environment

Branch `feature/build-006a-secure-crawler`, starting SHA `fc1418a`. The test-only
utility is `backend/tests/integration/crawler_faults.py`. No production lifecycle,
admission limit, advisory lock, or migration was changed. The existing untracked
`frontend/tsconfig.tsbuildinfo` was preserved.

Tests ran in a disposable PostgreSQL 16 container with a 512 MiB tmpfs, an
isolated Docker network, and a Python test container limited to 512 MiB RSS and
two CPUs. `postgres_ci.py` verified the exact test database identity and applied
Alembic through 0007. No website request or production service was used.

## Injection contract

`FaultPlan` targets one enumerated boundary and fires once at a configured
positive occurrence. `verify()` fails if the boundary was missed. `fixture_id`
uses a fixed UUID namespace and the seed `r3-a-v1`; each case has a reproducible
job ID. Every asynchronous operation has a five-second timeout. The existing
fixture cleans its rows in `finally` and disposes the engine.

`FaultSessionFactory` attaches SQLAlchemy listeners only to each test session.
It does not patch the global Session class. The adapter delegates actual claims,
renewals, recovery and persistence to `PostgresCrawlerJobRepository`, so tests
exercise its real transactions and independent PostgreSQL sessions.

| Boundary | Hook | Expected durable state after fault |
|---|---|---|
| Before claim | Adapter entry | Queued, no attempt |
| After attempt creation | `after_flush`, before commit | Queued, no attempt after rollback |
| Immediately before claim commit | `before_commit` | Queued, no attempt after rollback |
| After claim commit | `after_commit` | Running, one attempt |
| Before renewal | Adapter entry | Running, unchanged |
| Before result persistence | Adapter entry | Running, no source |
| During result transaction | `before_flush` with source pending | Running, no source after rollback |
| Ambiguous result acknowledgment | `after_commit` | Completed, one source despite client error |
| During retry scheduling | `before_flush` on queued retry | Original running attempt preserved after rollback |
| During cancellation | `before_flush` on cancelled job | Original running attempt preserved after rollback |

The ambiguous-ack hook injects a client-visible error **after SQLAlchemy has
committed**. It proves that a caller cannot infer rollback from that error and
must query authoritative state. It does not simulate a network packet loss.
Connection interruption and process termination need the real PostgreSQL and
subprocess tests in R3-B/C. No arbitrary external side-effect exactly-once
claim follows from this framework.

## Executed evidence

- `timeout 90 pytest -q tests/integration/test_crawler_fault_framework.py` in
  the disposable test container: **10 passed in 1.82 s**.
- `ruff check tests/integration/crawler_faults.py tests/integration/test_crawler_fault_framework.py`:
  passed.
- `mypy tests/integration/crawler_faults.py tests/integration/test_crawler_fault_framework.py --follow-imports=silent`:
  passed, no issues in two files.

The case assertions read persisted job, attempt and source rows through a fresh
session. They distinguish pre-commit rollback from post-commit client error.
R3-A is **PASS for its framework and focused boundary evidence**. R3-B, R3-C
and R3-D remain unassessed at this checkpoint.
