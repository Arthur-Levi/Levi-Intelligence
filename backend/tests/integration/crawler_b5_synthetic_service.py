"""Controlled B.5 crawler target; copied into a disposable worker image only."""

import asyncio
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit


class UnsafeWebsiteError(ValueError):
    pass


async def verify_and_extract(website_url: str) -> dict:
    parsed = urlsplit(website_url)
    if parsed.scheme != "https" or parsed.hostname != "synthetic.example.test":
        raise UnsafeWebsiteError("synthetic target only")
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2 or not re.fullmatch(r"[a-z0-9-]{1,64}", parts[1]):
        raise UnsafeWebsiteError("invalid synthetic target")
    mode, identity = parts
    if mode == "hold":
        marker = Path(f"/tmp/b5-release-{identity}")
        while not marker.exists():
            await asyncio.sleep(0.05)
    elif mode == "finish":
        await asyncio.sleep(0.05)
    elif mode == "stall":
        while True:
            time.sleep(1)
    elif mode == "cpu":
        while True:
            pass
    elif mode == "crash":
        raise RuntimeError("synthetic failure with secret-looking text")
    else:
        raise UnsafeWebsiteError("unknown synthetic target")
    return {
        "status": "verified",
        "status_code": 200,
        "checked_at": datetime.now(UTC),
        "emails": [f"b5-{identity}@example.test"],
    }
