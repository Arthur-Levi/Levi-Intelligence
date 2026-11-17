# PostgreSQL integration tests

These tests are deliberately opt-in and must only use a disposable database whose name contains `test`.

```bash
# From repository root: starts an ephemeral PostgreSQL instance (no persistent volume).
docker compose --profile test up -d postgres-test

cd backend
export TEST_DATABASE_URL='postgresql+asyncpg://levi_test:levi_test_only@localhost:55432/levi_intelligence_test'
export DATABASE_URL="$TEST_DATABASE_URL"
alembic upgrade head
pytest -q tests/integration
```

The integration test wraps its fixture inserts in a transaction and rolls it back. It does not drop,
truncate, or reset any tables. Stop the disposable service with `docker compose --profile test down`.
Never point `TEST_DATABASE_URL` at a development, staging, or production database.
