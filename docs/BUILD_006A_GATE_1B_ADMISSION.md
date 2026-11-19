# Build 006A — Gate 1B.2 Admission Validation

## Architecture

`AdmissionController` coordinates request admission within one Python process and
one asyncio event loop. It uses a single condition-protected state transition to
reserve global capacity, per-host capacity, and one rate token before returning an
admission. `SecureHTTPTransport.fetch()` sends every HTTP hop through
`_request_admitted()`, which holds the permit while `_request_once()` performs the
request. Redirects are revalidated and acquire a new permit for each hop.
`company_enrichment.verify_and_extract()` uses `SecureHTTPTransport.fetch()`.

The default controller is cached on the running event loop and reused by transport
instances in that loop. Explicitly supplied controllers bind to their first loop
and raise `RuntimeError` if used from another loop. Separate loops receive separate
default controllers.

## Defaults and queue policy

| Setting | Default |
|---|---:|
| Concurrent requests per process/event loop | 16 |
| Concurrent requests per hostname | 2 |
| Queued waiters | 64 |
| Tracked hostname states | 2,048 |
| Rate | 1 request/second per hostname |
| Token bucket burst | 2 |
| Maximum admission wait | 30 seconds |

Waiters are kept in a bounded deque. New requests cannot jump ahead of queued
requests. The scheduler selects the earliest waiter that currently has both global
and per-host capacity and a rate token, allowing another hostname to use otherwise
idle capacity. A full queue, expired wait, or full host-state table fails closed
with `AdmissionRejectedError`.

Inactive host state is removed only when there are no active requests or waiters
and its token bucket has fully replenished. This preserves rate history while
bounding the table; if no safe entry can be evicted, new hostnames are rejected.

## Rate limiting and cancellation

Each hostname has a monotonic-clock token bucket. Admission consumes a token, and
each redirect hop consumes another token for its destination hostname. Rate
limiting is process-local and is not a distributed quota.

Cancellation while waiting removes that waiter and wakes peers. Once a permit has
been granted, the async context manager runs release in a shielded child task. It
waits for release to finish even if cancellation is repeated, then propagates
cancellation. Exceptions from the request body also pass through the same release
path.

## Integration and security boundary

The HTTP transport's only `aiohttp` request call is inside `_request_once()`, and
the supported `fetch()` path invokes it only from `_request_admitted()`. Redirects
use this path for each request. The transport continues to validate each URL,
re-resolve each destination through `DNSGuard`/`PinnedResolver`, require TLS, and
disable environment proxy use. The synthetic tests do not establish live network,
TLS, DNS, routing, or egress behavior.

Admission state is local to one process and event loop. Multiple worker processes
do not share concurrency, queue, hostname, or rate state. Distributed protection is
NOT VERIFIED and is not provided by this implementation. Gate 1B.2 does not add or
claim robots.txt behavior.

## Validation evidence

Executed from `backend/`, without internet access, real sockets, or a persistent
database:

| Command | Actual result |
|---|---|
| `pytest -q tests/intelligence/crawler/test_admission.py` | PASS — 19 passed |
| `pytest -q tests/intelligence/crawler/test_http_transport.py` | PASS — 30 passed |
| `pytest -q tests/intelligence/crawler/test_company_enrichment.py` | PASS — 2 passed |
| `pytest -q tests/intelligence/crawler` | PASS — 155 passed |
| `ruff check app/intelligence/crawler/admission.py app/intelligence/crawler/http_transport.py app/services/company_enrichment.py tests/intelligence/crawler/test_admission.py tests/intelligence/crawler/test_admission_integration.py` | PASS — no findings |
| `mypy app/intelligence/crawler/admission.py app/intelligence/crawler/http_transport.py app/services/company_enrichment.py` | PASS — no issues in 3 source files |

Synthetic transport tests cover company enrichment's controlled path, global and
per-host concurrency, capacity shared by different hosts, per-hop redirect
admission, exception cleanup, and cancellation cleanup. Admission tests also cover
limits, queue saturation, cancellation during wait and release, rate refill,
bounded hostname state, high concurrency, and loop affinity.

The workspace already contained staged crawler and documentation changes before
this gate. The 155-test crawler result reflects that full local workspace; those
pre-existing changes are preserved separately from this gate's commit where they
are outside its scope.

## Limitations

- Limits are per process and per event loop, not service-wide.
- The hostname table intentionally rejects new hosts when all entries are active,
  queued, or still carry rate debt.
- In-process tests verify control flow and synthetic concurrency only; they do not
  verify sockets, TLS handshakes, resolver behavior against live DNS, or egress
  enforcement.
- Robots policy is outside this gate's validation scope.
