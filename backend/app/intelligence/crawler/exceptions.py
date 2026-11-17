
"""Exceptions raised by the secure crawler."""


class CrawlerSecurityError(Exception):
    """Base exception for crawler security policy violations."""


class UnsafeTargetError(CrawlerSecurityError):
    """Raised when a URL or resolved address is not safe to access."""


class DNSResolutionError(CrawlerSecurityError):
    """Raised when a hostname cannot be safely resolved."""


class RobotsDeniedError(CrawlerSecurityError):
    """Raised when robots.txt disallows crawling a resource."""


class ResponseTooLargeError(CrawlerSecurityError):
    """Raised when a response exceeds the configured byte limit."""


class TooManyRedirectsError(CrawlerSecurityError):
    """Raised when the redirect limit is exceeded."""


class FetchTimeoutError(CrawlerSecurityError):
    """Raised when a crawler request exceeds its timeout."""


class UnsupportedContentTypeError(CrawlerSecurityError):
    """Raised when a response has an unsupported content type."""
