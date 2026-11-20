"""Unit tests for fail-closed URL parsing; these tests never connect to a database."""

import pytest

from scripts.postgres_ci import test_database_url as load_test_database_url

SAFE_TEST_URL = (
    "postgresql+asyncpg://levi_gate_1b1_ci:synthetic@postgres:5432/levi_gate_1b1_ci"
)


def test_requires_explicit_test_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="must be explicitly configured"):
        load_test_database_url()


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
