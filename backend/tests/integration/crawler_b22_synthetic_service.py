"""Disposable container-only service mounted over company_enrichment.py."""

import asyncio
import signal
import sys
import time
from datetime import UTC, datetime


class UnsafeWebsiteError(ValueError):
    pass


async def verify_and_extract(website_url: str) -> dict:
    print("b22-synthetic-started", file=sys.stderr, flush=True)
    mode = website_url.rsplit("/", 1)[-1]
    if mode == "finish":
        await asyncio.sleep(0.2)
    elif mode == "block":
        while True:
            time.sleep(1)
    elif mode == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(1)
    elif mode == "ignore_cancel":
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                continue
    else:
        await asyncio.Event().wait()
    return {"status": "verified", "status_code": 200, "checked_at": datetime.now(UTC)}
