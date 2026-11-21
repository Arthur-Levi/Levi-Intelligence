"""Supervised, per-attempt crawler subprocess with bounded JSON IPC."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from app.crawler_jobs.contracts import CrawlerJobResult
from app.services.company_enrichment import UnsafeWebsiteError

MAX_INPUT_BYTES = 1024
MAX_OUTPUT_BYTES = 8192
PROTOCOL_VERSION = 1


class AttemptTimedOut(TimeoutError):
    """The parent's absolute attempt deadline expired."""


class InvalidResponseError(ValueError):
    """The child's bounded reply failed protocol validation."""


class ChildTerminationError(RuntimeError):
    """The direct child could not be reaped within the termination budget."""


def _decode_response(data: bytes, attempt_id: UUID) -> dict[str, object]:
    if not data.endswith(b"\n") or data.count(b"\n") != 1:
        raise InvalidResponseError("incomplete child response")

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON value")

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("duplicate JSON field")
        return dict(pairs)

    try:
        message = json.loads(data, parse_constant=reject_constant, object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InvalidResponseError("invalid child response") from exc
    if (not isinstance(message, dict) or type(message.get("version")) is not int
            or message["version"] != PROTOCOL_VERSION):
        raise InvalidResponseError("invalid child protocol version")
    if message.get("attempt_id") != str(attempt_id):
        raise InvalidResponseError("child response attempt mismatch")
    kind = message.get("kind")
    if kind == "error":
        if set(message) != {"version", "attempt_id", "kind", "code"}:
            raise InvalidResponseError("invalid child error response")
        if message["code"] == "unsafe_website":
            raise UnsafeWebsiteError("unsafe_website")
        if message["code"] == "crawler_error":
            raise RuntimeError("crawler_error")
        raise InvalidResponseError("invalid child failure code")
    if kind != "result" or set(message) != {"version", "attempt_id", "kind", "result"}:
        raise InvalidResponseError("invalid child result response")
    try:
        result = CrawlerJobResult.model_validate(message["result"])
    except ValidationError as exc:
        raise InvalidResponseError("invalid child result schema") from exc
    return result.model_dump(mode="python")


class ProcessCrawler:
    """Launch one child; never hand it a DB object, lease token or tenant identity."""

    def __init__(
        self,
        *,
        attempt_id: UUID,
        deadline: float,
        termination_seconds: float,
        executor_path: Path | None = None,
    ) -> None:
        if termination_seconds <= 0:
            raise ValueError("termination_seconds must be positive")
        self.attempt_id = attempt_id
        self.deadline = deadline
        self.termination_seconds = termination_seconds
        self.executor_path = executor_path
        self.child_pid: int | None = None

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AttemptTimedOut
        return remaining

    @staticmethod
    def _signal_group(pid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass

    @staticmethod
    def _exit_status(pid: int) -> os.waitid_result | None:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)

    @staticmethod
    def _group_has_live_members(pgid: int) -> bool:
        # The supported runtime is Linux. Zombies cannot execute and are
        # reaped by their parent or the container init after termination.
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                fields = (entry / "stat").read_text().rpartition(")")[2].split()
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if len(fields) >= 3 and int(fields[2]) == pgid and fields[0] != "Z":
                return True
        return False

    async def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        # Keep the direct child unreaped until after group signals. Its reserved
        # PID then prevents this process group ID from being reused.
        self._signal_group(process.pid, signal.SIGTERM)
        term_grace = min(1.0, self.termination_seconds / 2)
        grace_end = time.monotonic() + term_grace
        while self._exit_status(process.pid) is None and time.monotonic() < grace_end:
            await asyncio.sleep(min(0.01, max(0, grace_end - time.monotonic())))
        self._signal_group(process.pid, signal.SIGKILL)
        kill_end = time.monotonic() + max(0.1, self.termination_seconds - term_grace)
        while (self._exit_status(process.pid) is None
               or self._group_has_live_members(process.pid)) and time.monotonic() < kill_end:
            await asyncio.sleep(min(0.01, max(0, kill_end - time.monotonic())))
        if self._exit_status(process.pid) is None or self._group_has_live_members(process.pid):
            raise ChildTerminationError("child group was not stopped and reaped")
        process.wait(timeout=0)
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()

    async def __call__(self, url: str) -> dict[str, object]:
        request = {
            "version": PROTOCOL_VERSION,
            "attempt_id": str(self.attempt_id),
            "url": url,
        }
        payload = (json.dumps(request, separators=(",", ":")) + "\n").encode()
        if len(payload) > MAX_INPUT_BYTES:
            raise ValueError("child request exceeds input limit")
        self._remaining()
        command = (
            [sys.executable, str(self.executor_path)] if self.executor_path is not None
            else [sys.executable, "-m", "app.crawler_jobs.executor"]
        )
        # Popen returns an owned handle before the first await. Cancellation
        # cannot land between OS spawn and process ownership.
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={
                "PATH": os.defpath,
                "LANG": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            },
            close_fds=True,
            start_new_session=True,
        )
        self.child_pid = process.pid
        try:
            assert process.stdin is not None and process.stdout is not None
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            sent = 0
            while sent < len(payload):
                remaining = self._remaining()
                try:
                    sent += os.write(process.stdin.fileno(), payload[sent:])
                except BlockingIOError:
                    await asyncio.sleep(min(0.01, remaining))
            process.stdin.close()
            response = bytearray()
            while True:
                remaining = self._remaining()
                try:
                    chunk = os.read(
                        process.stdout.fileno(), min(1024, MAX_OUTPUT_BYTES + 1 - len(response))
                    )
                except BlockingIOError:
                    await asyncio.sleep(min(0.01, remaining))
                    continue
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > MAX_OUTPUT_BYTES:
                    raise InvalidResponseError("child response exceeds output limit")
            status = self._exit_status(process.pid)
            while status is None:
                await asyncio.sleep(min(0.01, self._remaining()))
                status = self._exit_status(process.pid)
            if status.si_code != os.CLD_EXITED or status.si_status != 0:
                raise RuntimeError("crawler child exited without success")
            self._remaining()
            result = _decode_response(bytes(response), self.attempt_id)
            self._remaining()
            return result
        except TimeoutError as exc:
            raise AttemptTimedOut from exc
        finally:
            await self._terminate(process)
