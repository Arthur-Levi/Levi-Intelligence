# SEC-OPS Gate 1B.1-CI — Isolated PostgreSQL Integration Environment

**Repository:** `Arthur-Levi/Levi-Intelligence`  
**Branch:** `feature/build-006a-secure-crawler`  
**Decision:** **BLOCKED — CI EXECUTION NOT VERIFIED**

## Workflow audit and changes

The repository contained one file under `.github/workflows`: `.github/workflows/ci`.
It declared Python 3.12, `pip install -e '.[dev]'`, `ruff check app tests`,
`pytest -q`, and a Node 22 frontend build. It had no PostgreSQL service,
migration step, or configured database environment variables. The file had no
`.yml` or `.yaml` extension, which GitHub requires to recognize a workflow.
It is now `.github/workflows/ci.yml`; the declared backend and frontend checks
were preserved. The ordinary backend job excludes `tests/integration` because
those tests now run in the dedicated database job; no non-integration test is
excluded from that job's pytest command.

The previous integration test used `TEST_DATABASE_URL` but skipped when it was
unset. Integration collection now fails closed without that variable and runs
a database identity preflight before test collection proceeds.

## Isolated service declaration

The new `postgres-integration` job runs in a Python 3.12 job container and
declares a PostgreSQL 16 service named `postgres`. The job and service share
the GitHub Actions job network, so the database URL uses service hostname
`postgres` and container port 5432; no host or external database endpoint is
configured. The service receives a dedicated synthetic database/user/password
from workflow configuration. PostgreSQL's data directory is explicitly mounted
as a 512 MiB `tmpfs`, with no persistent volume. No production secrets or
repository `.env` file are used. The service exists only for this job;
GitHub Actions' service-container lifecycle discards job services after the job
ends. This is static workflow/lifecycle configuration evidence until a run is
observed.

The `TEST_DATABASE_URL` variable is scoped only to `postgres-integration`. The
preflight accepts only the expected `postgresql+asyncpg` scheme, service host,
port, exact synthetic database and user, and a non-empty password. It rejects
other hosts (including local or production-like hosts), database names,
users, schemes, ports, and URL query parameters. It queries
`current_database()`, `current_user`, and `version()` and requires the expected
identity and PostgreSQL 16. It prints database/user/server identity only, never
the connection URL or password.

Before migration, `scripts/postgres_ci.py` verifies identity. It then passes
the already-validated URL to Alembic through the process-only
`ALEMBIC_TEST_DATABASE_URL` override. `backend/alembic/env.py` uses this
explicit override when present and otherwise retains the existing application
settings behavior. After upgrade it repeats the identity check, verifies the
single expected Alembic head, and checks `crawler_jobs` lease/retry columns.
The application database default was not changed.

## Commands and local validation

The CI job runs these commands from `backend/`, in order:

```bash
python scripts/postgres_ci.py preflight
python scripts/postgres_ci.py migrate-and-verify
pytest -q tests/integration
```

Local, database-free checks performed while preparing this change:

| Command | Result |
|---|---|
| `ruff check backend/alembic/env.py backend/scripts/postgres_ci.py backend/tests/test_postgres_ci_config.py backend/tests/integration` | Passed. |
| `mypy alembic/env.py scripts/postgres_ci.py app/crawler_jobs app/workers/crawler.py` (from `backend/`) | Passed; 7 source files. |
| `python -m compileall -q alembic/env.py scripts/postgres_ci.py tests/test_postgres_ci_config.py tests/integration` (from `backend/`) | Passed. |
| `pytest -q tests/test_postgres_ci_config.py` (from `backend/`) | 7 passed; URL parsing only, no database connections. |
| `env -u TEST_DATABASE_URL pytest -q tests/integration` (from `backend/`) | Expected fail-closed result: exit 4 with `TEST_DATABASE_URL is required`; no connection attempted. |
| `pytest -q --ignore=tests/integration --ignore=tests/test_health.py` (from `backend/`) | 211 passed. Health tests were excluded only for this local run because they stall in this environment; the CI backend command retains them. |
| `pytest -q --ignore=tests/integration` (from `backend/`) | Interrupted after reaching 100% progress because the process did not exit in this environment. No result is claimed for this command. |
| `ruff check app tests` (from `backend/`) | Failed on 246 repository lint findings, including pre-existing files outside this change. The existing workflow check was preserved. |
| `env -u TEST_DATABASE_URL python scripts/postgres_ci.py preflight` (from `backend/`) | Expected fail-closed result: exit 1, `PostgreSQL CI check failed: ValueError`; no connection attempted. |
| `git diff --check` | Passed. |

`actionlint` was not installed locally. No Docker daemon, database, migration,
or PostgreSQL integration test was run from Codespaces.

## CI execution evidence and decision

**GitHub Actions run ID/result:** not yet available at the time this report was
prepared. Static YAML and local checks do not prove that GitHub started the
service, passed the identity preflight, applied migrations, ran the adapter
smoke test, or discarded the service container.

The `postgres-integration` job includes:

- A preflight before any migration.
- Alembic upgrade and expected head/schema verification.
- A real PostgreSQL adapter claim smoke test using synthetic tenant, user,
  company, and job rows.
- A transaction rollback check for synthetic rows.
- Test cleanup of only the synthetic rows created by that smoke test.

No preserved database was connected to or modified. No full Gate 1B.1 lifecycle
acceptance is claimed.

**BLOCKED — CI EXECUTION NOT VERIFIED**
