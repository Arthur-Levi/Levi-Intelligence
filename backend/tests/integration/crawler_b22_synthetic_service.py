"""Disposable container-only service mounted over company_enrichment.py."""

import asyncio
import os
from datetime import UTC, datetime


class UnsafeWebsiteError(ValueError):
    pass


async def verify_and_extract(_website_url: str) -> dict:
    print("b22-synthetic-started", flush=True)
    mode = os.environ.get("B22_SYNTHETIC_MODE", "wait")
    if mode == "finish":
        await asyncio.sleep(0.2)
    elif mode == "ignore_cancel":
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                continue
    else:
        await asyncio.Event().wait()
    return {"status": "verified", "status_code": 200, "checked_at": datetime.now(UTC)}
