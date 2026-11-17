import asyncio
import ipaddress
import re
import socket
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?:\+?\d[\d().\s-]{7,}\d)")


class UnsafeWebsiteError(ValueError):
    pass


def _public_host(hostname: str) -> bool:
    try:
        records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not records:
        return False
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if not address.is_global:
            return False
    return True


async def verify_and_extract(website_url: str) -> dict:
    parsed = urlsplit(website_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeWebsiteError("Website must use HTTP or HTTPS")
    if parsed.username or parsed.password or not await asyncio.to_thread(_public_host, parsed.hostname):
        raise UnsafeWebsiteError("Website host is not publicly reachable")

    timeout = httpx.Timeout(8.0, connect=4.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers={"User-Agent": "LeviIntelligenceBot/1.0 (+business data verification)"}) as client:
        response = await client.get(website_url)
    if response.status_code >= 400:
        return {"status": "http_error", "status_code": response.status_code, "checked_at": datetime.now(UTC)}
    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type.lower():
        return {"status": "reachable_non_html", "status_code": response.status_code, "checked_at": datetime.now(UTC)}

    html = response.text[:500_000]
    emails = sorted({item.lower() for item in EMAIL_RE.findall(html) if not item.lower().endswith((".png", ".jpg", ".webp"))})
    phones = sorted({re.sub(r"\s+", " ", item).strip() for item in PHONE_RE.findall(html)})
    return {
        "status": "verified",
        "status_code": response.status_code,
        "emails": emails[:10],
        "phones": phones[:10],
        "checked_at": datetime.now(UTC),
    }
