import re
from datetime import UTC, datetime

from app.intelligence.crawler.exceptions import CrawlerSecurityError
from app.intelligence.crawler.http_transport import SecureHTTPTransport
from app.intelligence.crawler.url_validator import validate_url

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?:\+?\d[\d().\s-]{7,}\d)")


class UnsafeWebsiteError(ValueError):
    pass


async def verify_and_extract(website_url: str) -> dict:
    try:
        target = validate_url(website_url)
        response = await SecureHTTPTransport().fetch(target.url)
    except CrawlerSecurityError as exc:
        raise UnsafeWebsiteError("Website URL or DNS destination is not safe") from exc
    if response.status >= 400:
        return {
            "status": "http_error",
            "status_code": response.status,
            "checked_at": datetime.now(UTC),
        }
    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type.lower():
        return {
            "status": "reachable_non_html",
            "status_code": response.status,
            "checked_at": datetime.now(UTC),
        }

    html = response.body.decode("utf-8", errors="replace")
    emails = sorted(
        {
            item.lower()
            for item in EMAIL_RE.findall(html)
            if not item.lower().endswith((".png", ".jpg", ".webp"))
        }
    )
    phones = sorted({re.sub(r"\s+", " ", item).strip() for item in PHONE_RE.findall(html)})
    return {
        "status": "verified",
        "status_code": response.status,
        "emails": emails[:10],
        "phones": phones[:10],
        "checked_at": datetime.now(UTC),
    }
