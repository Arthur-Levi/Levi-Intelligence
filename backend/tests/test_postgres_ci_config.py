"""Unit tests for fail-closed URL parsing; these tests never connect to a database."""

import pytest

import scripts.postgres_ci as postgres_ci
from scripts.postgres_ci import test_database_url as load_test_database_url

SAFE_TEST_URL = "postgresql+asyncpg://levi_gate_1b1_ci:synthetic@postgres:5432/levi_gate_1b1_ci"


def test_requires_explicit_test_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="must be explicitly configured"):
        load_test_database_url()


def test_migrate_and_verify_runs_alembic_outside_async_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.engine import make_url

    url = make_url("postgresql+asyncpg://levi_gate_1b1_ci:secret@postgres:5432/levi_gate_1b1_ci")
    calls: list[str] = []

    async def verify_identity(_url: object) -> None:
        calls.append("identity")

    async def verify_schema(_url: object, _config: object) -> None:
        calls.append("schema")

    def upgrade(_config: object, revision: str) -> None:
        import asyncio

        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        assert revision == "head"
        calls.append("upgrade")

    monkeypatch.setattr(postgres_ci, "test_database_url", lambda: url)
    monkeypatch.setattr(postgres_ci, "alembic_config", lambda _url: object())
    monkeypatch.setattr(postgres_ci, "verify_identity", verify_identity)
    monkeypatch.setattr(postgres_ci, "verify_schema", verify_schema)
    monkeypatch.setattr(postgres_ci.command, "upgrade", upgrade)

    postgres_ci.main("migrate-and-verify")

    assert calls == ["identity", "upgrade", "identity", "schema"]


def test_accepts_only_expected_ci_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", SAFE_TEST_URL)
    url = load_test_database_url()
    assert url.host == "postgres"
    assert url.database == "levi_gate_1b1_ci"
    assert url.username == "levi_gate_1b1_ci"


@pytest.mark.parametrize(
    "unsafe_url",
    [
        SAFE_TEST_URL.replace("@postgres:", "@localhost:"),
        SAFE_TEST_URL.replace("@postgres:", "@production-db:"),
        SAFE_TEST_URL.replace("/levi_gate_1b1_ci", "/levi_intelligence"),
        SAFE_TEST_URL.replace("levi_gate_1b1_ci:synthetic", "levi:synthetic"),
        SAFE_TEST_URL.replace("postgresql+asyncpg", "postgresql+psycopg"),
    ],
)
def test_rejects_non_ci_or_development_database_urls(
    monkeypatch: pytest.MonkeyPatch, unsafe_url: str
) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", unsafe_url)
    with pytest.raises(ValueError, match="does not identify the isolated CI PostgreSQL service"):
        load_test_database_url()
