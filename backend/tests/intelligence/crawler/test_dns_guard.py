"""Unit tests for DNS address validation and connection pinning."""

from __future__ import annotations

import asyncio
import socket
import ssl

import aiohttp
import pytest
from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL

from app.intelligence.crawler.dns_guard import (
    DNSGuard,
    PinnedResolver,
    validate_resolved_addresses,
)
from app.intelligence.crawler.exceptions import DNSResolutionError, UnsafeTargetError


def record(address: str) -> ResolveResult:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return ResolveResult(
        hostname="example.com",
        host=address,
        port=443,
        family=family,
        proto=socket.IPPROTO_TCP,
        flags=0,
    )


class FakeResolver(AbstractResolver):
    def __init__(self, responses: list[list[ResolveResult] | OSError]) -> None:
        self.responses = iter(responses)

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[ResolveResult]:
        response = next(self.responses)
        if isinstance(response, OSError):
            raise response
        return response

    async def close(self) -> None:
        return None


@pytest.mark.parametrize("address", ["8.8.8.8", "2606:4700:4700::1111"])
def test_accepts_public_ipv4_and_ipv6(address: str) -> None:
    assert validate_resolved_addresses([record(address)]) == (address,)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1",
        "169.254.1.1", "0.0.0.0", "100.64.0.1", "192.0.2.1",
        "198.18.0.1", "224.0.0.1", "255.255.255.255",
        "::", "::1", "fc00::1", "fd00::1", "fe80::1", "ff02::1",
        "::ffff:127.0.0.1", "::ffff:10.0.0.1",
    ],
)
def test_rejects_private_and_link_local_addresses(address: str) -> None:
    with pytest.raises(UnsafeTargetError):
        validate_resolved_addresses([record(address)])


def test_validates_all_multiple_dns_answers_and_deduplicates() -> None:
    assert validate_resolved_addresses(
        [record("8.8.8.8"), record("2606:4700:4700::1111"), record("8.8.8.8")]
    ) == ("8.8.8.8", "2606:4700:4700::1111")


def test_rejects_mixed_public_and_private_dns_answers() -> None:
    with pytest.raises(UnsafeTargetError):
        validate_resolved_addresses([record("8.8.8.8"), record("192.168.1.10")])


def test_rejects_empty_dns_answer() -> None:
    with pytest.raises(DNSResolutionError):
        validate_resolved_addresses([])


@pytest.mark.parametrize(
    "addresses",
    [
        ["8.8.8.8", "10.0.0.1"],
        ["192.168.1.1", "127.0.0.1"],
        ["fe80::1", "fd00::1"],
    ],
)
def test_rejects_any_answer_containing_non_global_address(addresses: list[str]) -> None:
    with pytest.raises(UnsafeTargetError):
        validate_resolved_addresses([record(address) for address in addresses])


def test_rejects_scoped_dns_address() -> None:
    with pytest.raises(DNSResolutionError, match="scoped"):
        validate_resolved_addresses([record("2001:4860::1%eth0")])


@pytest.mark.asyncio
async def test_wraps_resolution_failure() -> None:
    with pytest.raises(DNSResolutionError, match="resolution failed"):
        await DNSGuard(FakeResolver([socket.gaierror("simulated failure")])).resolve(
            "example.com", 443
        )


@pytest.mark.asyncio
async def test_rejects_dns_rebinding_answer_on_each_resolution() -> None:
    guard = DNSGuard(FakeResolver([[record("8.8.8.8")], [record("127.0.0.1")]]))
    assert await guard.resolve("example.com", 443) == ("8.8.8.8",)
    with pytest.raises(UnsafeTargetError):
        await guard.resolve("example.com", 443)


@pytest.mark.asyncio
async def test_connector_connects_to_validated_ip_with_original_tls_hostname() -> None:
    guard = DNSGuard(
        FakeResolver([[record("8.8.8.8"), record("2606:4700:4700::1111")]])
    )
    connector = aiohttp.TCPConnector(
        resolver=PinnedResolver(guard), use_dns_cache=False, force_close=True, ssl=True
    )
    connected: dict[str, object] = {}

    async def capture_connection(factory: object, **kwargs: object) -> tuple[object, object]:
        connected.update(kwargs)
        return object(), object()

    connector._wrap_create_connection = capture_connection  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()
    request = aiohttp.ClientRequest("GET", URL("https://example.com/"), loop=loop, ssl=True)
    try:
        await connector._create_direct_connection(
            request, [], aiohttp.ClientTimeout(total=1.0)
        )
    finally:
        await connector.close()

    # These are the exact arguments passed into aiohttp's socket/TLS connection
    # routine: the socket target is the validated IP, while SNI and Host keep the DNS name.
    addr_infos = connected["addr_infos"]
    assert len(addr_infos) == 1  # type: ignore[arg-type]
    assert addr_infos[0][4][0] == "8.8.8.8"  # type: ignore[index]
    assert connected["server_hostname"] == "example.com"
    tls_context = connected["ssl"]
    assert isinstance(tls_context, ssl.SSLContext)
    assert tls_context.check_hostname
    assert tls_context.verify_mode == ssl.CERT_REQUIRED
    assert request.headers["Host"] == "example.com"
