import pytest

from app.crawler_jobs.db_resources import CrawlerDatabaseResources


def test_default_pool_matches_worker_concurrency_without_overflow(monkeypatch) -> None:
    for name in (
        "CRAWLER_WORKER_CONCURRENCY", "CRAWLER_DB_POOL_SIZE", "CRAWLER_DB_MAX_OVERFLOW",
        "CRAWLER_DB_POOL_TIMEOUT_SECONDS", "CRAWLER_DB_CONNECT_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    resources = CrawlerDatabaseResources.from_env()
    assert resources.concurrency == resources.pool_size == 2
    assert resources.max_overflow == 0
    assert resources.pool_timeout_seconds == 2
    assert resources.connect_timeout_seconds == 3
    assert resources.server_settings == {
        "statement_timeout": "5000",
        "lock_timeout": "1000",
        "idle_in_transaction_session_timeout": "5000",
    }


@pytest.mark.parametrize("name,value", [
    ("CRAWLER_WORKER_CONCURRENCY", "0"),
    ("CRAWLER_WORKER_CONCURRENCY", "nan"),
    ("CRAWLER_DB_POOL_SIZE", "1"),
    ("CRAWLER_DB_MAX_OVERFLOW", "1"),
    ("CRAWLER_DB_POOL_TIMEOUT_SECONDS", "inf"),
    ("CRAWLER_DB_POOL_TIMEOUT_SECONDS", "-1"),
    ("CRAWLER_DB_CONNECT_TIMEOUT_SECONDS", "nan"),
    ("CRAWLER_DB_LOCK_TIMEOUT_SECONDS", "6"),
    ("CRAWLER_DB_STATEMENT_TIMEOUT_SECONDS", "7"),
    ("CRAWLER_DB_IDLE_TRANSACTION_TIMEOUT_SECONDS", "20"),
    ("CRAWLER_DB_COMMAND_TIMEOUT_SECONDS", "4"),
    ("CRAWLER_DB_TRANSACTION_TIMEOUT_SECONDS", "5"),
])
def test_invalid_database_limits_are_rejected_at_startup(monkeypatch, name, value) -> None:
    for setting in (
        "CRAWLER_WORKER_CONCURRENCY", "CRAWLER_DB_POOL_SIZE", "CRAWLER_DB_MAX_OVERFLOW",
        "CRAWLER_DB_POOL_TIMEOUT_SECONDS", "CRAWLER_DB_CONNECT_TIMEOUT_SECONDS",
        "CRAWLER_DB_LOCK_TIMEOUT_SECONDS", "CRAWLER_DB_STATEMENT_TIMEOUT_SECONDS",
        "CRAWLER_DB_IDLE_TRANSACTION_TIMEOUT_SECONDS", "CRAWLER_DB_COMMAND_TIMEOUT_SECONDS",
        "CRAWLER_DB_TRANSACTION_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(setting, raising=False)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        CrawlerDatabaseResources.from_env()
