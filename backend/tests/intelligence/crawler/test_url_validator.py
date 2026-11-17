
"""Tests for secure crawler URL validation."""

import pytest

from app.intelligence.crawler.exceptions import UnsafeTargetError
from app.intelligence.crawler.url_validator import validate_url


@pytest.mark.parametrize(
    ("url", "expected_scheme", "expected_host", "expected_port"),
    [
        ("https://example.com", "https", "example.com", 443),
        ("http://example.com/path", "http", "example.com", 80),
        ("https://EXAMPLE.COM/about", "https", "example.com", 443),
        ("https://example.com:443/search?q=dental",
         "https", "example.com", 443),
    ],
)
def test_accepts_valid_public_url(
    url: str,
    expected_scheme: str,
    expected_host: str,
    expected_port: int,
) -> None:
    result = validate_url(url)

    assert result.scheme == expected_scheme
    assert result.hostname == expected_host
    assert result.port == expected_port


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "ftp://example.com",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "//example.com/path",
        "https://",
        "https://user:password@example.com",
        "https://example.com:8080",
        "http://example.com:443",
        "https://localhost",
        "http://localhost.local",
        "http://service.internal",
        "http://service.test",
        "http://127.0.0.1",
        "http://10.0.0.1",
        "http://192.168.1.1",
        "http://169.254.169.254",
        "http://[::1]",
        "http://[fc00::1]",
        "http://127.1",
        "http://2130706433",
        "https://example.com\\@127.0.0.1",
        " https://example.com",
        "https://example.com ",
    ],
)
def test_rejects_unsafe_or_invalid_url(url: str) -> None:
    with pytest.raises(UnsafeTargetError):
        validate_url(url)


def test_normalizes_hostname_and_empty_path() -> None:
    result = validate_url("HTTPS://Example.COM")

    assert result.url == "https://example.com/"
    assert result.hostname == "example.com"
    assert result.path == "/"


def test_removes_fragment() -> None:
    result = validate_url("https://example.com/page#section")

    assert result.url == "https://example.com/page"


def test_preserves_query_string() -> None:
    result = validate_url("https://example.com/search?q=dental&page=2")

    assert result.query == "q=dental&page=2"
    assert result.url == "https://example.com/search?q=dental&page=2"


def test_rejects_non_string_input() -> None:
    with pytest.raises(UnsafeTargetError):
        validate_url(None)  # type: ignore[arg-type]


def test_rejects_excessively_long_url() -> None:
    url = "https://example.com/" + ("a" * 2_100)

    with pytest.raises(UnsafeTargetError):
        validate_url(url)
