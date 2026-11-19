"""HTTP transport with DNS pinning and manually revalidated redirects."""

from __future__ import annotations

import math
from dataclasses import dataclass
from urllib.parse import urljoin

import aiohttp

from .admission import AdmissionController, get_default_admission_controller
from .dns_guard import DNSGuard, PinnedResolver
from .exceptions import (
    FetchError,
    FetchTimeoutError,
    ResponseTooLargeError,
    TooManyRedirectsError,
)
from .url_validator import ValidatedURL, validate_url

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """Bounded response returned after a secure HTTP request."""

    url: str
    status: int
    headers: dict[str, str]
    body: bytes


class SecureHTTPTransport:
    """Fetch HTTP(S) URLs using only DNS-validated, connection-pinned addresses."""

    def __init__(
        self,
        *,
        dns_guard: DNSGuard | None = None,
        timeout_seconds: float = 8.0,
        max_response_bytes: int = 500_000,
        max_redirects: int = 5,
        admission_controller: AdmissionController | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be greater than zero")
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")

        self._dns_guard = dns_guard
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_redirects = max_redirects
        self._admission = admission_controller

    async def fetch(self, url: str) -> FetchResponse:
        current = validate_url(url)
        for redirect_count in range(self._max_redirects + 1):
            response = await self._request_admitted(current)
            location = response.headers.get("location")
            if response.status not in REDIRECT_STATUSES or location is None:
                return response
            if redirect_count == self._max_redirects:
                raise TooManyRedirectsError("Redirect limit exceeded")
            current = validate_url(urljoin(current.url, location))
        raise TooManyRedirectsError("Redirect limit exceeded")

    async def _request_admitted(self, target: ValidatedURL) -> FetchResponse:
        admission = self._admission or get_default_admission_controller()
        async with admission.request(target.hostname):
            return await self._request_once(target)

    async def _request_once(self, target: ValidatedURL) -> FetchResponse:
        timeout = aiohttp.ClientTimeout(
            total=self._timeout_seconds,
            sock_connect=min(4.0, self._timeout_seconds),
            sock_read=self._timeout_seconds,
        )
        owns_guard = self._dns_guard is None
        dns_guard = self._dns_guard or DNSGuard()
        connector: aiohttp.TCPConnector | None = None
        try:
            connector = aiohttp.TCPConnector(
                resolver=PinnedResolver(dns_guard),
                use_dns_cache=False,
                force_close=True,
                ssl=True,
            )
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                trust_env=False,
                headers={"User-Agent": "LeviIntelligenceBot/1.0 (+business data verification)"},
            ) as session:
                async with session.get(target.url, allow_redirects=False) as response:
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > self._max_response_bytes:
                            raise ResponseTooLargeError("Response exceeds configured byte limit")
                        chunks.append(chunk)
                    return FetchResponse(
                        url=target.url,
                        status=response.status,
                        headers={key.lower(): value for key, value in response.headers.items()},
                        body=b"".join(chunks),
                    )
        except TimeoutError as exc:
            raise FetchTimeoutError("Crawler request timed out") from exc
        except aiohttp.ClientError as exc:
            raise FetchError("Crawler HTTP request failed") from exc
        finally:
            # Covers failures while constructing ClientSession as well as
            # cancellation/errors before its context manager takes ownership.
            if connector is not None and not connector.closed:
                await connector.close()
            if owns_guard:
                await dns_guard.close()
