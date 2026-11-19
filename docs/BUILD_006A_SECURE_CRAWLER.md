# Build 006A — Secure Crawler

## HTTP and DNS architecture

The previous `company_enrichment.py` implementation resolved a hostname with
`socket.getaddrinfo`, checked the returned addresses, then made a separate
`httpx.AsyncClient` request. HTTPX uses HTTP Core to resolve and open the socket
later. Because that second resolution was not tied to the checked answers, a DNS
change between the check and connection could send the request to a private or
otherwise forbidden address.

Build 006A uses `aiohttp` with its asynchronous c-ares resolver (`aiodns`) behind
`DNSGuard` and `PinnedResolver`. DNS Guard validates every returned IPv4 and IPv6
address with Python's `ipaddress` global-address policy. An empty, malformed,
failed, or mixed public/non-public answer is rejected. The custom resolver gives
aiohttp one validated numeric address; aiohttp receives no unchecked hostname
for a later DNS lookup. The original URL hostname remains in the request, so
HTTP `Host`, TLS SNI, and certificate hostname verification use the DNS name.
TLS verification remains enabled with the library defaults.

The transport disables environment proxy discovery and DNS caching, closes each
connection after a request, limits response bytes and time, and disables automatic
redirects. Redirects are followed in application code only after resolving the
new location against the prior URL and passing it through URL validation; every
hop gets a new validated DNS resolution and pinned connection. Company website
verification now uses this transport instead of its prior separate HTTPX
resolution/request path. HTTPX remains in use for unrelated integrations such as
Apify.

## Decisions and limitations

- `aiohttp` exposes a resolver hook that participates in connection setup. The
  existing HTTPX client does not expose an equivalent public per-client resolver
  and pinned-address hook.
- Every answer in a DNS response must be global. The transport selects the first
  validated answer for that connection rather than racing across answers.
- `aiodns`/c-ares uses configured DNS servers, not the OS `getaddrinfo` path. Hosts
  file aliases and some platform-specific search behavior may therefore differ.
- Network egress controls remain useful defense in depth. This work does not
  control routing changes or a compromised DNS server returning a globally
  routable address that is not the intended origin.
- Unit tests use synthetic resolver answers and synthetic responses. They make no
  requests to real websites and do not use a database.

## Transport review and expanded tests

The deeper transport review found that `aiohttp.ClientError` failures outside
timeouts could escape the crawler exception hierarchy. This included connection
failures and response payload/protocol errors, which callers such as company
enrichment do not handle as crawler errors. The transport now wraps these as
`FetchError`; timeout exceptions remain mapped to `FetchTimeoutError`.

Transport tests now cover streaming at and over the byte cap, bodies without a
declared length, malformed/truncated payload errors, connection and read timeout
translation, connection failure translation, redirects across hostnames with a
new DNS resolution per hop, rejected redirect schemes, redirect chain limits,
and session/connector/owned-resolver cleanup on failure. All new transport tests
use mocks; no socket is opened. The test resolver returns a TEST-NET address
and is never used to establish a connection. Existing DNS tests cover
mixed and non-public IPv4/IPv6 answers, rebinding responses, and preservation of
the request hostname for Host and TLS verification.

The content-length mismatch case simulates the `aiohttp.ClientPayloadError`
reported when a response body is shorter than declared. The transport's byte cap
is enforced on streamed chunks regardless of `Content-Length`; this suite does
not perform raw wire-level HTTP parser testing. No real sites, private services,
or persistent databases were accessed.

## Validation results

Results from the transport review run:

- `ruff check app/intelligence/crawler/exceptions.py
  app/intelligence/crawler/http_transport.py
  tests/intelligence/crawler/test_http_transport.py` — passed.
- `mypy app/intelligence/crawler app/services/company_enrichment.py` — passed,
  6 source files checked.
- `pytest -q tests/intelligence/crawler` — passed, 62 tests.
- `pytest -q --ignore=tests/test_health.py` — passed, 79 tests; 1 PostgreSQL
  integration test skipped because `TEST_DATABASE_URL` was not configured.
- An initial local-server attempt failed before exercising the transport: 9
  tests failed to bind loopback, while the other 53 crawler tests passed. The
  restricted Codespace disallows that bind. Those cases were changed to
  socket-free mocks; the final validation results above are from that suite.
