
"""Strict URL validation for the secure crawler.

This module performs syntactic validation only.
DNS resolution and connection pinning belong to dns_guard.py
and the HTTP transport layer.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .exceptions import UnsafeTargetError


MAX_URL_LENGTH = 2_048
ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = {
    "http": frozenset({80}),
    "https": frozenset({443}),
}
BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".test",
    ".invalid",
    ".example",
)
HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True, slots=True)
class ValidatedURL:
    """Normalized URL components accepted by the crawler."""

    url: str
    scheme: str
    hostname: str
    port: int
    path: str
    query: str


def _reject(message: str) -> None:
    raise UnsafeTargetError(message)


def _normalize_hostname(hostname: str) -> str:
    """Convert a hostname to lowercase ASCII IDNA form and validate it."""
    if not hostname:
        _reject("URL must contain a hostname")

    if "%" in hostname:
        _reject("Percent-encoded or scoped hostnames are not allowed")

    hostname = hostname.rstrip(".").lower()

    if not hostname:
        _reject("Hostname is empty")

    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        _reject("IP-literal targets are not allowed; use a public DNS hostname")

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UnsafeTargetError("Hostname is not valid IDNA") from exc

    if len(ascii_hostname) > 253:
        _reject("Hostname exceeds the maximum length")

    if ascii_hostname == "localhost" or any(
        ascii_hostname.endswith(suffix) for suffix in BLOCKED_HOST_SUFFIXES
    ):
        _reject("Local or reserved hostname suffixes are not allowed")

    # Reject ambiguous numeric host forms such as 127.1 or 2130706433.
    if re.fullmatch(r"[0-9.]+", ascii_hostname):
        _reject("Ambiguous numeric hostnames are not allowed")

    labels = ascii_hostname.split(".")
    if len(labels) < 2:
        _reject("A fully qualified public hostname is required")

    if any(not HOST_LABEL_RE.fullmatch(label) for label in labels):
        _reject("Hostname contains an invalid DNS label")

    if any(len(label) > 63 for label in labels):
        _reject("Hostname label exceeds 63 characters")

    return ascii_hostname


def _validate_ip_literal(hostname: str) -> None:
    """Reject IP literals and any non-global address."""
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return

    if not address.is_global:
        _reject("Non-public IP addresses are not allowed")

    # This crawler requires DNS names so that TLS hostname validation
    # and DNS pinning can be applied consistently.
    _reject("IP-literal targets are not supported")


def _parse_url(url: str) -> SplitResult:
    if not isinstance(url, str):
        _reject("URL must be a string")

    if not url or len(url) > MAX_URL_LENGTH:
        _reject("URL is empty or exceeds the maximum length")

    if url != url.strip():
        _reject("Leading or trailing whitespace is not allowed")

    if "\\" in url:
        _reject("Backslashes are not allowed in URLs")

    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        _reject("Control characters are not allowed in URLs")

    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise UnsafeTargetError("URL could not be parsed safely") from exc

    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        _reject("Only HTTP and HTTPS URLs are allowed")

    if not parsed.netloc or not parsed.hostname:
        _reject("URL must contain a hostname")

    if parsed.username is not None or parsed.password is not None:
        _reject("URLs containing credentials are not allowed")

    if parsed.fragment:
        # Fragments are client-side identifiers and are not sent to servers.
        parsed = parsed._replace(fragment="")

    return parsed


def validate_url(url: str) -> ValidatedURL:
    """Validate and normalize a URL before any network access.

    This function does not resolve DNS and does not make network requests.
    Every resolved address must be checked separately by dns_guard.py.
    """
    parsed = _parse_url(url)
    scheme = parsed.scheme.lower()

    try:
        raw_port = parsed.port
    except ValueError as exc:
        raise UnsafeTargetError("URL contains an invalid port") from exc

    port = raw_port if raw_port is not None else (
        443 if scheme == "https" else 80
    )

    if port not in ALLOWED_PORTS[scheme]:
        _reject(f"Port {port} is not allowed for {scheme}")

    hostname = _normalize_hostname(parsed.hostname or "")
    _validate_ip_literal(hostname)

    path = parsed.path or "/"
    if not path.startswith("/"):
        _reject("URL path must be absolute")

    netloc = hostname
    normalized_url = urlunsplit(
        (scheme, netloc, path, parsed.query, "")
    )

    if len(normalized_url) > MAX_URL_LENGTH:
        _reject("Normalized URL exceeds the maximum length")

    return ValidatedURL(
        url=normalized_url,
        scheme=scheme,
        hostname=hostname,
        port=port,
        path=path,
        query=parsed.query,
    )
