# PostgreSQL integration tests

These integration tests run in the `postgres-integration` GitHub Actions job
against its job-scoped PostgreSQL 16 service container. The job uses the
service hostname `postgres`, the synthetic database/user configured in the
workflow, and an ephemeral container with no persistent volume. GitHub Actions
discards the service container when the job completes.

The test suite requires an explicit `TEST_DATABASE_URL`. Before migrations and
again before pytest starts, `scripts/postgres_ci.py preflight` checks that the
URL uses the expected PostgreSQL driver, service hostname, port, exact test
database and user, then verifies `current_database()`, `current_user`, and
PostgreSQL 16. It never falls back to the application's default database URL.
The only supported integration execution sequence is the one encoded in
`.github/workflows/ci.yml`:

```bash
python scripts/postgres_ci.py preflight
python scripts/postgres_ci.py migrate-and-verify
pytest -q tests/integration
```

The migration command passes the already-validated URL to Alembic through the
current process environment. It then checks the single Alembic head and the
crawler lease columns before pytest runs. Integration fixtures use synthetic
tenants, users, companies, and jobs and remove their own rows; rollback checks
assert that uncommitted synthetic rows are absent.

Do not point these tests at a local, development, staging, or production
database. A local Compose service is not accepted by the preflight because its
host is not the GitHub Actions service hostname.
