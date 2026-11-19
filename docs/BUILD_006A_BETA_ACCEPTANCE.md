# Build 006A — Beta Acceptance, Sec-Ops Gate 1

**Project:** Levi Intelligence
**Branch:** `feature/build-006a-secure-crawler`
**Initial checkpoint:** `b094fea22623120dd1002d090751b3c182120114`
**Gate:** controlled security review; no live crawling or network probing

## Gate 1 scope

Assess the crawler's URL and DNS policy, redirect behavior, bounded resource use,
cleanup under errors/cancellation, and operational controls. All acceptance tests
must use synthetic input, controlled resolvers, mocked transport, and no persistent
database. This gate does not establish safety against real network routing, DNS
infrastructure, or deployment configuration.

## Threat model

### Assets to protect

- Internal services, loopback listeners, private and link-local hosts, and cloud
  metadata endpoints from crawler-originated requests.
- Crawler process availability, memory, sockets, resolver/session resources, and
  caller isolation from hostile or oversized responses.
- Target hostname integrity for HTTP Host, TLS SNI, and certificate validation.

### Trust boundaries

- Input URLs, redirect locations, DNS answers, response headers, and response body
  bytes are untrusted.
- `validate_url` is a syntax/policy boundary; DNS answers are separately checked
  by `DNSGuard` and passed to aiohttp through `PinnedResolver`.
- The host/container network, DNS service, routing, runtime limits, and deployment
  configuration are outside this repository's demonstrated boundary.
- Unit-test mocks prove code-path behavior only; they do not prove network egress
  enforcement or live socket behavior.

### Attack vectors considered

Malformed/ambiguous hostnames and URL encodings; IPv4/IPv6 special-use literals
returned by DNS; empty, failed, malformed, mixed, or changing DNS answers; redirects
to another host or forbidden address; response streaming beyond the byte cap; slow
or cancelled requests; connector/session/resolver initialization and cleanup errors.

## Control inventory

| Control | Status | Evidence / limitation |
|---|---|---|
| HTTP(S)-only URL parsing, hostname and port policy | IMPLEMENTED | `url_validator.py` |
| DNS global-address validation of every answer | IMPLEMENTED | `dns_guard.py`; includes mixed answer rejection |
| Pinned resolver, no DNS cache, no environment proxy, force-close | IMPLEMENTED | `http_transport.py`; code inspection only |
| Redirects revalidated and separately resolved | IMPLEMENTED | Application-managed redirect loop |
| Total/connect/read timeouts and streamed response byte cap | IMPLEMENTED | aiohttp timeout configuration and chunk accounting |
| Session, connector, and owned resolver cleanup | IMPLEMENTED | Cleanup paths; exception paths covered by tests |
| Cancellation cleanup | TESTED | Synthetic blocked stream is cancelled; session, connector, and owned guard close |
| IPv4/IPv6 adversarial DNS matrix and URL variations | TESTED | Synthetic resolver/unit inputs; no connection attempted |
| Network behavior against real sockets/TLS/DNS | NOT VERIFIED | Prohibited in this gate and unavailable in restricted environment |
| Global/per-domain concurrency and pending-task caps | NOT IMPLEMENTED | No admission-control mechanism found |
| Rate limiting | NOT IMPLEMENTED | No limiter found |
| robots.txt enforcement | NOT IMPLEMENTED | Exception type exists, enforcement does not |
| Stable crawler identity | IMPLEMENTED | Fixed User-Agent in transport; delivery not independently verified |
| Egress/network isolation and host resource limits | NOT VERIFIED | Requires deployment/host evidence |

## Adversarial test matrix

| Area | Cases | Method | Acceptance |
|---|---|---|---|
| IPv4/IPv6 DNS | Loopback, private, link-local, reserved/non-global, IPv4-mapped IPv6 | Synthetic `ResolveResult` | Every non-global answer rejected |
| DNS response shape | Empty, resolver error, malformed/scoped, mixed public/private, multiple non-public | Controlled resolver | Fail closed before socket access |
| Redirects | Different host, private destination, same-host rebinding, repeat resolution, scheme rejection, hop cap | Mocked response sequence plus controlled resolver | Every hop revalidated and re-resolved |
| URL parsing | Credentials, IP literals, numeric aliases, malformed labels/ports, control chars, backslashes, encoding | Pure unit input | Rejected before transport |
| Resource bounds | total/connect/read timeout, streamed bytes, payload error | Mocked aiohttp boundaries | Bounded result or typed error |
| Cleanup | connector/session init failure, fetch exception, cancellation, owned/injected guard | Fakes and cancellation task | Owned resources close; injected guard remains caller-owned |

Mocks and synthetic answers are not evidence of network-level SSRF prevention.

## Operational acceptance and egress requirements

The execution environment must provide and document all of the following before
beta release:

- Outbound network policy blocks loopback, private, link-local, and reserved
  destinations, including IPv4-mapped IPv6 and cloud metadata endpoints.
- Explicit egress allow/deny policy, with enforcement outside application URL
  validation and protection against route changes after DNS resolution.
- Crawler process/container isolation from application data services and internal
  control planes; no Docker socket access.
- Enforced CPU, memory, file descriptor, socket, and task limits.
- Per-domain and global concurrency limits, pending-work limits, and rate limits.
- robots.txt policy and a documented crawler identity/contact policy.

No firewall, Codespace network, host, or infrastructure configuration was changed
for this gate. These controls are NOT VERIFIED from repository inspection.

## Objective decision criteria

- **PASS**: implementation and test evidence exist for the stated control, with no
  claim beyond the test boundary.
- **NO-GO**: a required control fails, or a reproducible vulnerability is found.
- **BLOCKED**: a required verification cannot run because the technical or
  environmental evidence is unavailable.
- **NOT IMPLEMENTED**: the required mechanism does not exist in this repository.
- Global beta approval requires no critical NO-GO/BLOCKED/NOT IMPLEMENTED item and
  separate deployment evidence for egress and runtime isolation.

## Evidence collected

Commands below ran from `backend/`. No external websites, private services, cloud
metadata endpoints, network sockets, or persistent databases were used.

| Command | Actual result | Scope / exclusions |
|---|---|---|
| `pytest -q tests/intelligence/crawler` | PASS — 109 passed | Synthetic resolver and mocked transport only |
| `ruff check app/intelligence/crawler tests/intelligence/crawler` | PASS | No lint findings |
| `mypy app/intelligence/crawler app/services/company_enrichment.py` | PASS — 6 source files checked | No type findings |
| `pytest -q --ignore=tests/test_health.py` | PASS — 126 passed, 1 skipped | PostgreSQL integration skipped because `TEST_DATABASE_URL` is unset; two health tests intentionally excluded because their `TestClient` opens loopback |
| `pytest -q` | INTERRUPTED at 56% while entering health tests | The `TestClient` health tests open loopback; stopped to honor the no-network test constraint. No final suite result is claimed. |

No test failures remain in the executed crawler or backend-excluding-health
commands. The intentionally excluded health tests were not run; the single
PostgreSQL integration test was skipped by its own guard because no dedicated test
database was configured. The earlier interrupted full-suite attempt is not counted
as a pass or a completed failure result.

During the initial crawler matrix, Python 3.14 reported `224.0.0.1` as
`ipaddress.is_global == True`. The original guard therefore accepted a multicast
answer. The guard now explicitly rejects multicast addresses, with IPv4 and IPv6
regression cases. This is a code-level finding; tests still do not prove live
network enforcement.

## Residual risks and beta release blockers

- Repository URL/DNS checks cannot constrain host routing or a compromised resolver
  returning an unintended globally routable address.
- The test suite uses mocks and cannot prove live TLS, DNS pinning, firewall
  enforcement, or routing behavior.
- No concurrency cap, pending-task limit, rate limit, or robots.txt enforcement
  exists in the reviewed crawler path.
- Deployment evidence for egress blocks, process isolation, Docker socket absence,
  and CPU/memory limits is required before beta release.

## Gate 1 checkpoint decision

**BLOCKED for beta approval.** DNS/URL policy, redirect handling, bounded response
streaming, and tested cleanup paths pass their available synthetic tests. The
multicast acceptance defect found in testing was corrected and its regression test
passes. Live network behavior and deployment egress/isolation remain NOT VERIFIED;
global/per-domain concurrency, pending-work and rate limits, and robots.txt
enforcement are NOT IMPLEMENTED. These blockers prevent global Gate 1 approval.
