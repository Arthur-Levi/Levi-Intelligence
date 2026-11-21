"""Unprivileged crawler child: one bounded request, one sanitized reply."""

import asyncio
import json
import sys
from uuid import UUID

from app.crawler_jobs.contracts import CrawlerJobResult
from app.crawler_jobs.process_executor import MAX_INPUT_BYTES, PROTOCOL_VERSION
from app.services.company_enrichment import UnsafeWebsiteError, verify_and_extract


def main() -> int:
    raw = sys.stdin.buffer.readline(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES or not raw.endswith(b"\n"):
        return 2
    try:
        request = json.loads(raw)
        if (
            not isinstance(request, dict)
            or set(request) != {"version", "attempt_id", "url"}
            or request["version"] != PROTOCOL_VERSION
            or not isinstance(request["url"], str)
            or len(request["url"]) > 500
        ):
            return 2
        attempt_id = str(UUID(request["attempt_id"]))
    except (ValueError, TypeError, KeyError):
        return 2
    try:
        result = CrawlerJobResult.model_validate(asyncio.run(verify_and_extract(request["url"])))
        response = {
            "version": PROTOCOL_VERSION, "attempt_id": attempt_id,
            "kind": "result", "result": result.model_dump(mode="json"),
        }
    except UnsafeWebsiteError:
        response = {
            "version": PROTOCOL_VERSION, "attempt_id": attempt_id,
            "kind": "error", "code": "unsafe_website",
        }
    except Exception:
        response = {
            "version": PROTOCOL_VERSION, "attempt_id": attempt_id,
            "kind": "error", "code": "crawler_error",
        }
    data = (json.dumps(response, separators=(",", ":")) + "\n").encode()
    if len(data) > 8192:
        return 2
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
