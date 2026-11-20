"""Fail closed unless the isolated GitHub Actions PostgreSQL identity is verified."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def pytest_sessionstart(session: pytest.Session) -> None:
    del session
    if not os.environ.get("TEST_DATABASE_URL"):
        raise pytest.UsageError("TEST_DATABASE_URL is required for PostgreSQL integration tests")

    backend = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "scripts/postgres_ci.py", "preflight"],
        cwd=backend,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        # Preflight output is designed to contain identity only, never connection credentials.
        details = (result.stdout + result.stderr).strip()
        raise pytest.UsageError(f"PostgreSQL identity preflight failed: {details}")
    print(result.stdout, end="")
