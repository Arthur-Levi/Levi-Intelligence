"""Public DNS resolution and an aiohttp resolver that pins a checked address."""

from __future__ import annotations

import ipaddress
import socket

from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import AsyncResolver

from .exceptions import DNSResolutionError, UnsafeTargetError


def validate_resolved_addresses(records: list[ResolveResult]) -> tuple[str, ...]:
    """Validate every returned address and return normalized IPs."""
    if not records:
        raise DNSResolutionError("Hostname did not resolve to any addresses")

    addresses: list[str] = []
    seen: set[str] = set()
    for record in records:
        try:
            raw_address = record["host"]
            if "%" in raw_address:
                raise DNSResolutionError("DNS returned a scoped IP address")
            address = ipaddress.ip_address(raw_address)
        except (IndexError, TypeError, ValueError) as exc:
            raise DNSResolutionError("DNS returned a malformed address") from exc
        # ipaddress treats multicast as globally scoped on some Python versions;
        # multicast is never a valid unicast destination for this crawler.
        if not address.is_global or address.is_multicast:
            raise UnsafeTargetError(f"DNS returned a non-public address: {address}")
        normalized = str(address)
        if normalized not in seen:
            seen.add(normalized)
            addresses.append(normalized)

    return tuple(addresses)


class DNSGuard:
    """Resolve hostnames while rejecting empty, malformed, and non-public answers."""

    def __init__(self, resolver: AbstractResolver | None = None) -> None:
        self._resolver = resolver or AsyncResolver()

    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        try:
            records = await self._resolver.resolve(hostname, port, socket.AF_UNSPEC)
        except OSError as exc:
            raise DNSResolutionError(f"DNS resolution failed for {hostname}") from exc
        return validate_resolved_addresses(records)

    async def close(self) -> None:
        await self._resolver.close()


class PinnedResolver(AbstractResolver):
    """aiohttp resolver that validates all answers and supplies one pinned IP.

    A fresh connector is created for each request, with DNS caching disabled and
    connections closed after use. The URL hostname remains unchanged, so aiohttp
    retains it for Host and TLS certificate/SNI processing.
    """

    def __init__(self, guard: DNSGuard | None = None) -> None:
        self._guard = guard or DNSGuard()

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[ResolveResult]:
        addresses = await self._guard.resolve(host, port)
        # Every DNS answer was checked. One validated address is returned so the
        # connector cannot race to an unchecked alternative or perform its own DNS.
        address = addresses[0]
        parsed = ipaddress.ip_address(address)
        return [
            ResolveResult(
                hostname=host,
                host=address,
                port=port,
                family=socket.AF_INET6 if parsed.version == 6 else socket.AF_INET,
                proto=socket.IPPROTO_TCP,
                flags=0,
            )
        ]

    async def close(self) -> None:
        # DNSGuard ownership belongs to the caller, since redirects may reuse it.
        return None
