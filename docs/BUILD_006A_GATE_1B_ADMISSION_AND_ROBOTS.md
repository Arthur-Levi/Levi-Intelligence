# Build 006A — Gate 1B: Admission Control and Crawl Policy

## Scope and architecture

All outbound crawler requests enter through `SecureHTTPTransport.fetch()`. The
transport validates the URL, asks `RobotsPolicy` for the origin decision, then
acquires admission for the hostname before each HTTP request. Redirect locations
are resolved against the current URL and validated before another hop. Every
redirected page hop gets its own robots check and admission; robots document
redirects also get URL validation and admission. The robots document fetch uses
the same `SecureHTTPTransport` request implementation, DNSGuard, pinned resolver,
TLS verification, timeout, response byte cap, and redirect limit. Only recursive
robots consultation is skipped for that internal policy-document fetch.

A robots document may redirect to another origin. Each `Location` is resolved
against the current document URL and passed through `validate_url()` before any
request to it; each resulting HTTP request independently enters admission and the
normal DNS/IP pinning protections. Redirects are manual and bounded by the
transport's `max_redirects` setting (default 5). The fetched rules remain cached
under the origin that requested `/robots.txt`, and are checked against that
origin's page URLs. The destination origin does not inherit those rules. Failure
to validate or retrieve a redirect destination, an unexpected response, or
exceeding the redirect limit fails closed before the original page is fetched.

`company_enrichment.verify_and_extract()` continues to call
`SecureHTTPTransport.fetch()`. That method now enforces both policies, so company
enrichment cannot take a direct transport path around them. A rejected robots or
admission decision is a `CrawlerSecurityError` and is surfaced as an unsafe
website result by the service.

## Admission configuration

The defaults in `AdmissionConfig` are:

| Setting | Default | Meaning |
|---|---:|---|
| `global_limit` | 16 | Simultaneous HTTP requests in the worker event loop |
| `per_host_limit` | 2 | Simultaneous requests for one normalized hostname |
| `max_waiters` | 64 | Maximum queued requests |
| `max_hosts` | 2,048 | Maximum tracked hostname states |
| `requests_per_second` | 1.0 | Host token refill rate |
| `burst` | 2 | Per-host token bucket capacity |
| `max_wait_seconds` | 30 | Maximum admission wait before explicit rejection |

Host keys are lower-case IDNA ASCII names without a final dot. The bounded host
table evicts only inactive entries with no waiters and a fully replenished token
bucket; otherwise new host state is rejected. Queue saturation, host-table
saturation, and wait timeout raise `AdmissionRejectedError`. Context-managed
requests release capacity after success, errors, or cancellation.

These controls are in-memory and scoped to one asyncio event loop in one worker
process. Workers with separate event loops/processes do not coordinate limits.
There is no Redis or distributed admission. The repository has no trusted tenant
context for crawler calls, so no tenant-specific isolation is claimed.

## Robots policy

The fixed crawler identity is `LeviIntelligenceBot` in both the HTTP User-Agent
header and policy matching. The policy supports user-agent groups, Allow, Disallow,
`*` wildcards, `$` end anchors, longest-match precedence (Allow wins equal-length
ties), and percent-encoding normalization for paths. If a matching product token
group exists, general `*` groups are not combined with it. This is a deliberately
limited parser, not a claim of full RFC 9309 conformance.

Status and parse behavior:

| robots response | Policy |
|---|---|
| HTTP 200, valid content | Apply parsed rules |
| HTTP 200, malformed UTF-8/directive | Deny all paths for that origin until cache expiry |
| HTTP 404 | Treat as no published restrictions |
| HTTP 401 or 403 | Deny all paths |
| HTTP 5xx, unexpected status, timeout, DNS/transport error | Fail closed; do not cache the failure |
| Body exceeds 500,000 bytes | Fail closed; do not cache the failure |

The parser rejects malformed directives conservatively by producing a deny-all
policy. It does not support every RFC extension or all RFC-specific parsing rules.

## Policy cache limits

The default cache uses an origin key (scheme, normalized hostname, effective
port), a 3,600 second TTL, at most 1,024 entries, at most 128 in-flight origins,
and at most 64 waiters per in-flight origin. Expired entries are removed during
cache access; adding entries evicts least-recently-used entries. Concurrent loads
for one origin share a single fetch. Failed fetches are not cached as permission.
Malformed content is cached only as a deny-all decision for the configured TTL.

The robots cache and admission controller are shared by transports using the same
event loop. Separate event loops and worker processes have independent state.

## Evidence and status

| Requirement | Status | Evidence boundary |
|---|---|---|
| Global/per-host concurrency and finite queue | IMPLEMENTED | `admission.py`; synthetic tests |
| Per-host token-bucket rate limiting | IMPLEMENTED | `admission.py`; controlled monotonic clock test |
| Cancellation and error release | TESTED | Admission and transport synthetic tests |
| Hostname normalization/state bound | IMPLEMENTED | `admission.py`; bounded-host test |
| robots policy and fixed identity | IMPLEMENTED | `robots_policy.py`; parser/status tests |
| Bounded TTL cache and single-flight loading | IMPLEMENTED | `robots_policy.py`; synthetic expiry/eviction/coalescing tests |
| Cross-origin robots redirect, requesting-origin rule application, and per-hop admission | TESTED | Synthetic transport test; cache remains keyed only by requesting origin |
| Unsafe robots redirect blocked before destination request | TESTED | Synthetic IP-literal redirect test |
| Robots redirect count limit and no-loop behavior | TESTED | Synthetic two-hop redirect with limit set to one |
| Redirect destination timeout fails closed before page request | TESTED | Synthetic timeout test |
| Secure transport and enrichment integration | IMPLEMENTED | `http_transport.py`, `company_enrichment.py`; synthetic integration tests |
| DNS-pinned real requests and live TLS behavior | NOT VERIFIED | No network request is made by this test evidence |
| Global limits across event loops/processes | NOT IMPLEMENTED | In-memory loop-local control only |
| Trusted tenant-level admission or policy isolation | NOT IMPLEMENTED | No trusted tenant context exists at this layer |
| Real egress isolation | NOT VERIFIED | Synthetic tests cannot prove deployment network policy |
| CPU, memory, and filesystem isolation | NOT VERIFIED | Requires runtime/deployment evidence; not changed here |
| Docker socket isolation | NOT VERIFIED | Requires host/runtime evidence; not changed here |
| Tenant isolation | NOT VERIFIED | No trusted tenant context or cross-tenant isolation evidence exists at this layer |

All tests for this gate use synthetic responses, controlled clocks, or mocked
transport. They do not demonstrate egress filtering, route safety, infrastructure
isolation, or distributed coordination.
