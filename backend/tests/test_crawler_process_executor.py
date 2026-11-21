"""Hard-deadline regressions; initially red until the process executor exists."""

import asyncio
import time
from pathlib import Path
from uuid import uuid4

import pytest
from app.crawler_jobs.process_executor import AttemptTimedOut, ProcessCrawler

FIXTURE = Path(__file__).with_name("crawler_process_fixture.py")


@pytest.mark.asyncio
async def test_sync_blocking_child_is_killed_without_blocking_parent() -> None:
    started = time.monotonic()
    crawler = ProcessCrawler(
        attempt_id=uuid4(), deadline=started + 0.3,
        termination_seconds=0.2, executor_path=FIXTURE,
    )
    with pytest.raises(AttemptTimedOut):
        await asyncio.wait_for(crawler("https://example.test/block"), 3)
    assert time.monotonic() - started < 3


@pytest.mark.asyncio
async def test_child_response_must_match_attempt_identity() -> None:
    crawler = ProcessCrawler(
        attempt_id=uuid4(), deadline=time.monotonic() + 2,
        termination_seconds=0.2, executor_path=FIXTURE,
    )
    with pytest.raises(ValueError, match="attempt"):
        await asyncio.wait_for(crawler("https://example.test/wrong-attempt"), 3)
