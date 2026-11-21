"""Supervised, per-attempt crawler subprocess with bounded JSON IPC."""

import asyncio
import json
import os
import signal
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
    try:
        message = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidResponseError("invalid child response") from exc
    if not isinstance(message, dict) or message.get("version") != PROTOCOL_VERSION:
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

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        self._signal_group(process.pid, signal.SIGTERM)
        term_grace = min(1.0, self.termination_seconds / 2)
        try:
            await asyncio.wait_for(process.wait(), timeout=term_grace)
        except TimeoutError:
            pass
        self._signal_group(process.pid, signal.SIGKILL)
        if process.returncode is None:
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=max(0.1, self.termination_seconds - term_grace)
                )
            except TimeoutError as exc:
                raise ChildTerminationError("child was not reaped") from exc

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
        process = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={
                    "PATH": os.defpath,
                    "LANG": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONUNBUFFERED": "1",
                },
                close_fds=True,
                start_new_session=True,
            ),
            timeout=self._remaining(),
        )
        self.child_pid = process.pid
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(payload)
            await asyncio.wait_for(process.stdin.drain(), timeout=self._remaining())
            process.stdin.close()
            response = bytearray()
            while True:
                chunk = await asyncio.wait_for(
                    process.stdout.read(min(1024, MAX_OUTPUT_BYTES + 1 - len(response))),
                    timeout=self._remaining(),
                )
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > MAX_OUTPUT_BYTES:
                    raise InvalidResponseError("child response exceeds output limit")
            await asyncio.wait_for(process.wait(), timeout=self._remaining())
            if process.returncode != 0:
                raise RuntimeError("crawler child exited without success")
            self._remaining()
            return _decode_response(bytes(response), self.attempt_id)
        except TimeoutError as exc:
            raise AttemptTimedOut from exc
        finally:
            await self._terminate(process)
