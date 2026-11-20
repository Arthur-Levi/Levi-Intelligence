"""Fail-closed PostgreSQL preflight and migration verification for GitHub Actions."""

import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

EXPECTED_DATABASE = "levi_gate_1b1_ci"
EXPECTED_USER = "levi_gate_1b1_ci"
EXPECTED_HOST = "postgres"
EXPECTED_PORT = 5432
EXPECTED_ALEMBIC_HEAD = "0006_crawler_worker_leases"
REQUIRED_CRAWLER_COLUMNS = {
    "attempts",
    "lease_token",
    "lease_expires_at",
    "heartbeat_at",
    "next_attempt_at",
}


def test_database_url() -> URL:
    raw_url = os.environ.get("TEST_DATABASE_URL")
    if not raw_url:
        raise ValueError("TEST_DATABASE_URL must be explicitly configured")
    try:
        url = make_url(raw_url)
    except Exception as exc:
        raise ValueError("TEST_DATABASE_URL is invalid") from exc

    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != EXPECTED_HOST
        or url.port != EXPECTED_PORT
        or url.database != EXPECTED_DATABASE
        or url.username != EXPECTED_USER
        or not url.password
        or url.query
    ):
        raise ValueError("TEST_DATABASE_URL does not identify the isolated CI PostgreSQL service")
    return url


async def verify_identity(url: URL) -> None:
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            database, user, server_version = (
                await connection.execute(
                    text("SELECT current_database(), current_user, version()")
                )
            ).one()
        if database != EXPECTED_DATABASE or user != EXPECTED_USER:
            raise ValueError("connected database identity does not match the CI test identity")
        if not str(server_version).startswith("PostgreSQL 16"):
            raise ValueError("connected server is not PostgreSQL 16")
    except ValueError:
        raise
    except Exception as exc:
        # Do not include exception text: drivers may include connection details.
        raise RuntimeError(f"PostgreSQL identity preflight failed ({type(exc).__name__})") from None
    finally:
        await engine.dispose()

    print(
        "PostgreSQL identity verified: "
        f"database={EXPECTED_DATABASE}, user={EXPECTED_USER}, server=PostgreSQL 16"
    )


def alembic_config(url: URL) -> Config:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    # This explicit test-only override bypasses application settings and .env.
    os.environ["ALEMBIC_TEST_DATABASE_URL"] = url.render_as_string(hide_password=False)
    return config


async def verify_schema(url: URL, config: Config) -> None:
    expected_head = ScriptDirectory.from_config(config).get_current_head()
    if expected_head != EXPECTED_ALEMBIC_HEAD:
        raise RuntimeError("repository Alembic head does not match the expected crawler migration")

    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            revision_result = await connection.execute(
                text("SELECT version_num FROM alembic_version")
            )
            revision_rows: Sequence[object] = revision_result.scalars().all()
            column_result = await connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'crawler_jobs'"
                )
            )
            column_rows: Sequence[object] = column_result.scalars().all()
            revisions = [str(value) for value in revision_rows]
            columns = [str(value) for value in column_rows]
        if revisions != [EXPECTED_ALEMBIC_HEAD]:
            raise RuntimeError("database is not at the expected single Alembic head")
        missing_columns = REQUIRED_CRAWLER_COLUMNS.difference(columns)
        if missing_columns:
            raise RuntimeError("crawler lease schema is incomplete")
    finally:
        await engine.dispose()
    print("Alembic head and crawler lease columns verified")


async def main(action: str) -> None:
    url = test_database_url()
    await verify_identity(url)
    if action == "preflight":
        return

    config = alembic_config(url)
    command.upgrade(config, "head")
    await verify_identity(url)
    await verify_schema(url, config)
    print("Migrations applied and crawler schema verified")


if __name__ == "__main__":
    selected_action = sys.argv[1] if len(sys.argv) == 2 else ""
    if selected_action not in {"preflight", "migrate-and-verify"}:
        raise SystemExit("usage: python scripts/postgres_ci.py {preflight|migrate-and-verify}")
    try:
        asyncio.run(main(selected_action))
    except Exception as exc:
        print(f"PostgreSQL CI check failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
