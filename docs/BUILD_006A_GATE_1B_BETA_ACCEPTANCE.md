# Build 006A — Gate 1B.5 Integrated Beta Acceptance

**Project:** Levi Intelligence — Business Acquisition Platform
**Branch:** `feature/build-006a-secure-crawler`
**Scope:** software-level integrated security acceptance; synthetic fixtures only

## 1. Scope and objectives

This gate inspected and exercised the existing URL validation, DNS guard and
pinned resolver, HTTP transport, admission controller, robots policy, cache and
single-flight behavior, cleanup paths, and Company Enrichment consumer. The
objective was to establish that these controls compose across request and
redirect paths and that blocked or failed retrievals cannot become extracted
business facts or successful enrichment results.

No external websites, uncontrolled network requests, or persistent databases
were used. This decision is limited to repository-level software behavior.

## 2. Architecture and execution flow inspected

The public Company Enrichment path is:

1. `verify_and_extract()` calls `validate_url()` before constructing a transport
   request. Unsafe URL/DNS/robots errors are mapped to `UnsafeWebsiteError`.
2. `SecureHTTPTransport.fetch()` validates and normalizes the input URL again.
3. `RobotsPolicy.check()` applies cached rules or starts/joins bounded
   per-origin single-flight retrieval. Retrieval of `robots.txt` skips recursive
   policy evaluation but uses the same URL validation, redirect loop, admission,
   DNS, TLS, and response bounds as page requests. Policy retrieval errors fail
   closed; a denied path does not proceed to page retrieval.
4. Each robots, page, and redirect HTTP hop enters
   `AdmissionController.request(hostname)` and releases its reservation through
   the async context manager on success, exception, or cancellation.
5. `_request_once()` creates a fresh aiohttp connector, sets `ssl=True`, disables
   DNS caching and environment proxies, and uses `PinnedResolver`. The resolver
   calls `DNSGuard` for that hop, rejects every non-global/multicast answer, and
   supplies one validated IP to the connector while retaining the URL hostname
   for Host and TLS hostname verification.
6. Redirects are followed manually. Each location is resolved against the
   current URL and revalidated; each next hop returns through robots policy and
   admission before another request. Robots redirects use the same validated,
   admitted transport path without recursively checking robots.
7. HTTP response bodies are consumed in chunks with a configured byte cap and
   total/connect/read timeout. Connector and owned resolver cleanup runs in a
   `finally` path.
8. Only a successful HTML response reaches the email/phone pattern extractor.
   Non-2xx responses return `http_error` without extracted fields; robots denial
   and crawler security failures raise. The prospecting endpoint records the
   source website URL and extracted values in `SourceRecord`. HTTP errors now
   leave `enrichment_status="failed"` and do not set `last_verified_at`.

## 3. Components and interfaces integrated

- `backend/app/intelligence/crawler/url_validator.py` — `validate_url()` and
  `ValidatedURL`.
- `backend/app/intelligence/crawler/dns_guard.py` — `DNSGuard`,
  `validate_resolved_addresses()`, and `PinnedResolver`.
- `backend/app/intelligence/crawler/http_transport.py` —
  `SecureHTTPTransport.fetch()`, `_fetch_robots_document()`,
  `_request_admitted()`, `_request_once()`, and `FetchResponse`.
- `backend/app/intelligence/crawler/admission.py` — bounded admission request
  context, global/per-host slots, queue and rate controls.
- `backend/app/intelligence/crawler/robots_policy.py` — `RobotsPolicy.check()`,
  finite TTL/LRU cache, single-flight and cleanup behavior.
- `backend/app/services/company_enrichment.py` — `verify_and_extract()`.
- `backend/app/api/v1/endpoints/prospecting.py` — company enrichment status,
  persisted fields, and source record integration.

## 4. Security invariants and evidence

| Invariant | Evidence and result |
|---|---|
| URL syntax/policy is checked before a request | URL matrix and integrated transport tests reject unsupported schemes, credentials, invalid ports, malformed and local/numeric hosts. Invalid URLs fail before DNS/request paths. |
| No connection to prohibited destinations; no DNS rebinding bypass | Synthetic DNS matrix rejects loopback, private, link-local, reserved, multicast, mapped IPv6 and mixed answers. Re-resolution tests reject changed answers. Resolver/connector test asserts the exact socket destination is a checked IP and TLS/Host retain the DNS name. No real socket is opened. |
| Redirect cannot bypass validation | Same-origin and cross-origin redirect coverage, unsafe IP/port/scheme rejection, loop/limit checks, and robots redirect destination checks. Every redirect is revalidated. |
| Every HTTP hop is admitted | Transport integration tests assert admission on page and redirect hops, per-host/global caps, rejection before transport, and release after exceptions/cancellation. Robots fetches use the same admitted request helper. |
| Page retrieval obeys robots | Allow/disallow, fail-closed status/parse/retrieval errors, redirect handling, cache, single-flight, and the integrated page-not-fetched-on-denial test. Company integration regression confirms a denial does not reach extraction. |
| Responses and concurrency state are bounded | Chunk-size enforcement, timeouts, robots response cap, cache/inflight/waiter/pending/admission caps, cancellation, cleanup and recovery are covered by deterministic synthetic tests. |
| No stale reservation or in-flight robots operation after covered failures | Admission cancellation/exception tests and robots leader/waiter cancellation, repeated cancellation, failure/retry, and counter assertions. |
| Failed/prohibited responses do not become extracted facts | Company tests cover 404/503, robots denial, malformed bytes, missing metadata, and local target rejection. HTTP errors expose no email/phone lists. Endpoint regression asserts failed enrichment and no `last_verified_at`. |
| Extracted values retain source evidence and unavailable facts stay absent | Endpoint persists the source URL and captured extracted values in `SourceRecord`; missing matches remain empty and are not synthesized. Evidence is the fetched website source, not independent verification of ownership or deliverability. |
| Exceptions do not silently downgrade security | URL/DNS/robots errors are fail-closed; transport errors propagate to the endpoint's failure path; no insecure TLS/proxy fallback was found in the inspected path. |

## 5. Test scenarios and adversarial coverage

The existing crawler suite includes deterministic URL and DNS address matrices,
mixed answers and rebinding, pinned connector target plus TLS hostname checks,
mocked TLS/session/stream behavior, timeout and byte bounds, redirect targets and
limits, robots failures/denials/redirects/cache/single-flight, admission limits,
and cancellation/resource recovery. Cross-component transport tests exercise
robots-before-page, admission per redirect hop, blocked redirect destinations,
and Company Enrichment using the shared transport/admission path.

Gate 1B.5 adds Company Enrichment regressions for HTTP error responses,
robots-denied pages, malformed HTML/missing contact metadata, and the endpoint's
persisted failure status. Fixtures are synthetic and do not open external
connections.

Concurrency tests use events, bounded event-loop predicate helpers, held locks,
synthetic clocks, and controlled futures/responses. No arbitrary wall-clock
sleep is used to establish an expected state (bounded `asyncio.wait_for` calls
are only deadlock guards).

## 6. Exact validation evidence

Commands ran from `backend/` on the final implementation:

| Command | Result |
|---|---|
| `pytest -q tests/intelligence/crawler` | **177 passed**, 0 failed, 0 skipped (2.46s) |
| `pytest -q tests/test_discovery_enrichment.py` | **9 passed**, 0 failed, 0 skipped (0.56s) |
| `ruff check app/intelligence/crawler tests/intelligence/crawler` | **Passed**, all checks passed |
| `mypy app/intelligence/crawler` | **Passed**, no issues in 8 source files |

No warnings or skipped tests were reported by these final commands. An additional
lint attempt that included the entire pre-existing
`app/api/v1/endpoints/prospecting.py` reported numerous existing import-order,
line-length, and FastAPI `Depends` findings across that file. That broad lint
attempt is not the required crawler Ruff result and is not claimed as passing;
the endpoint change is exercised by the crawler integration regression test.

## 7. Defects discovered and corrections

The endpoint previously treated `http_error` as completed enrichment and set
`last_verified_at`, even though the service correctly returned no extracted
fields. This could make an unsuccessful page retrieval appear successful. The
endpoint now stores the HTTP status, marks enrichment failed, and leaves
`last_verified_at` unchanged. A focused regression test asserts this behavior.
No URL, DNS, transport, redirect, admission, or robots control was weakened.

## 8. Remaining limitations

- DNS, connection, TLS, redirects, and responses were tested with controlled
  synthetic resolvers/transports. The connector test checks aiohttp's generated
  connection arguments and TLS context; no live socket, DNS service, certificate
  chain, or routing behavior was exercised.
- `ipaddress.is_global` and application pinning do not control host routing,
  resolver compromise, NAT, or route changes after resolution.
- Admission and robots bounds are process/event-loop local, not distributed
  across workers. Upstream code can allocate dormant tasks before bounded
  coroutines start.
- The robots parser is conservative and limited; this gate does not establish
  complete RFC 9309 conformance.
- Email and phone extraction is pattern matching. A match is evidence that text
  appeared in the fetched page; it does not independently establish ownership,
  validity, consent, or deliverability.
- Full backend tests were not run. They include loopback `TestClient` behavior
  and database-dependent integrations outside this gate's isolated scope.

## 9. Unverified assumptions

- Production must provide egress restrictions and runtime isolation independently
  of application checks.
- Multiple crawler workers require shared limits if aggregate concurrency or
  rate budgets are required.
- Consumers must treat extracted email/phone strings as source-observed leads,
  not independently verified identity facts.

## 10. Infrastructure boundary

This acceptance provides software-level test evidence only. It does not prove
container/filesystem isolation, CPU or memory enforcement, egress firewalling,
Docker socket isolation, production queue isolation, worker identity separation,
or production abuse resistance. Those controls remain for SEC-OPS infrastructure
acceptance. This document does not declare the crawler production-ready.

## 11. Acceptance decision

**Decision: PASS** for the integrated software behavior tested in this gate.
Required request-path controls compose in the inspected implementation, the
HTTP-error enrichment status defect is corrected with a regression test, and the
final crawler, enrichment integration, Ruff, and Mypy commands pass. The PASS is
bounded to the synthetic repository-level evidence above; infrastructure and
live network behavior remain unverified and are not included in this decision.
