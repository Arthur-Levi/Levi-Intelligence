import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit


def normalize_company_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", value)


def normalize_website(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    candidate = value.strip()
    supplied_scheme = urlsplit(candidate).scheme.lower()
    if supplied_scheme and supplied_scheme not in {"http", "https"}:
        return None, None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None, None
    host = parsed.hostname.lower().rstrip(".")
    domain = host.removeprefix("www.")
    normalized = urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", "", ""))
    return normalized, domain
