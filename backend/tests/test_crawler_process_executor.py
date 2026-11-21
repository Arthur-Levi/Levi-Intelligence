"""Hard-deadline regressions; initially red until the process executor exists."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from app.crawler_jobs.process_executor import (
    AttemptTimedOut,
    InvalidResponseError,
    ProcessCrawler,
)

FIXTURE = Path(__file__).with_name("crawler_process_fixture.py")


def crawler(seconds: float = 2) -> ProcessCrawler:
    return ProcessCrawler(
        attempt_id=uuid4(), deadline=time.monotonic() + seconds,
        termination_seconds=0.2, executor_path=FIXTURE,
    )


def not_running(pid: int) -> bool:
    path = Path(f"/proc/{pid}/stat")
    return not path.exists() or path.read_text().split()[2] == "Z"


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
    process = crawler()
    with pytest.raises(ValueError, match="attempt"):
        await asyncio.wait_for(process("https://example.test/wrong-attempt"), 3)


@pytest.mark.asyncio
async def test_normal_child_result_is_validated_and_reaped() -> None:
    process = crawler()
    result = await asyncio.wait_for(process("https://example.test/normal"), 3)
    assert result["status"] == "verified" and result["status_code"] == 200
    assert process.child_pid is not None and not_running(process.child_pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cpu", "ignore-term"])
async def test_noncooperative_child_is_forcibly_terminated(mode: str) -> None:
    process = crawler(0.3)
    with pytest.raises(AttemptTimedOut):
        await asyncio.wait_for(process(f"https://example.test/{mode}"), 3)
    assert process.child_pid is not None and not_running(process.child_pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["malformed", "partial", "oversized", "duplicate",
                                      "bad-version", "bad-schema", "extra", "unknown-code",
                                      "nan"])
async def test_invalid_ipc_is_rejected(mode: str) -> None:
    with pytest.raises(InvalidResponseError):
        await asyncio.wait_for(crawler()(f"https://example.test/{mode}"), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["crash", "nonzero-success"])
async def test_child_failure_cannot_return_success(mode: str) -> None:
    with pytest.raises(RuntimeError, match="child exited"):
        await asyncio.wait_for(crawler()(f"https://example.test/{mode}"), 3)


@pytest.mark.asyncio
async def test_late_result_is_rejected() -> None:
    with pytest.raises(AttemptTimedOut):
        await asyncio.wait_for(crawler(0.2)("https://example.test/late"), 3)


@pytest.mark.asyncio
async def test_child_does_not_inherit_database_environment_or_descriptor(monkeypatch, tmp_path):
    monkeypatch.setenv("CRAWLER_DATABASE_URL", "synthetic-secret-marker")
    monkeypatch.setenv("TEST_DATABASE_URL", "synthetic-secret-marker")
    monkeypatch.setenv("B22_TEST_SECRET", "synthetic-secret-marker")
    marker = tmp_path / "inheritable-marker"
    with marker.open("w") as parent_file:
        os.set_inheritable(parent_file.fileno(), True)
        result = await asyncio.wait_for(
            crawler()(f"https://example.test/inspect-env?marker={marker}"), 3
        )
    assert result["status_code"] == 200


@pytest.mark.asyncio
async def test_cancellation_immediately_after_spawn_reaps_owned_child(monkeypatch) -> None:
    import app.crawler_jobs.process_executor as module

    original = module.subprocess.Popen
    task: asyncio.Task[dict[str, object]]

    def spawn_then_cancel(*args, **kwargs):
        process = original(*args, **kwargs)
        asyncio.get_running_loop().call_soon(task.cancel)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", spawn_then_cancel)
    process = crawler()
    task = asyncio.create_task(process("https://example.test/block"))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.child_pid is not None and not_running(process.child_pid)


@pytest.mark.asyncio
async def test_descendant_in_same_process_group_is_killed(tmp_path: Path) -> None:
    marker = tmp_path / "descendant.pid"
    process = crawler(0.5)
    with pytest.raises(AttemptTimedOut):
        await asyncio.wait_for(process(f"https://example.test/spawn?marker={marker}"), 3)
    assert marker.exists()
    descendant = int(marker.read_text())
    assert process.child_pid is not None and not_running(process.child_pid)
    assert not_running(descendant)


@pytest.mark.asyncio
async def test_successful_child_cannot_leave_live_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "descendant-success.pid"
    process = crawler()
    result = await asyncio.wait_for(
        process(f"https://example.test/spawn-then-result?marker={marker}"), 3
    )
    assert result["status"] == "verified"
    assert marker.exists() and not_running(int(marker.read_text()))


@pytest.mark.asyncio
async def test_maximum_ascii_url_and_bounded_result_fit_protocol() -> None:
    url = "https://example.test/" + "a" * (500 - len("https://example.test/"))
    assert len(url) == 500
    assert (await asyncio.wait_for(crawler()(url), 3))["status"] == "verified"
    result = await asyncio.wait_for(crawler()("https://example.test/max-result"), 3)
    assert len(result["emails"]) == 10 and len(result["phones"]) == 10


@pytest.mark.parametrize("payload", [
    b'{"version":true,"attempt_id":"x","url":"https://example.test"}\n',
    b'{"version":1,"version":1,"attempt_id":"x","url":"https://example.test"}\n',
    b'{"version":1,"attempt_id":"x","url":"https://example.test","lease":"x"}\n',
])
def test_real_child_rejects_invalid_request_without_crawling(payload: bytes) -> None:
    child = subprocess.run(
        [sys.executable, "-m", "app.crawler_jobs.executor"],
        input=payload, capture_output=True, timeout=3, check=False,
    )
    assert child.returncode == 2 and child.stdout == b""
