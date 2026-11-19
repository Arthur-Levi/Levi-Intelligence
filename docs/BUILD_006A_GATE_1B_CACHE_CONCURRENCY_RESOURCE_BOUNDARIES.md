# Build 006A — Gate 1B.4: Cache, Concurrency and Resource Boundaries

## Scope and audited architecture

Audited all modules in `backend/app/intelligence/crawler/`, all tests in
`backend/tests/intelligence/crawler/`, and
`docs/BUILD_006A_GATE_1B_ADMISSION_AND_ROBOTS.md`. The request path is
`SecureHTTPTransport.fetch()` → URL validation → robots policy → admission for
each HTTP hop → DNSGuard/PinnedResolver and bounded response reading. Robots
redirects also use validated, admitted HTTP requests without recursive policy
checks. Failures remain fail-closed; no DNS, TLS, admission, or robots bypass was
introduced.

The robots cache is an `OrderedDict` keyed by normalized origin. Successful
decisions, including deny-all for malformed documents, receive TTL; retrieval,
size, status, and cancellation failures are not cached. Inflight work is an
origin map with one future and a bounded waiter counter. Admission uses a
bounded host-state map, an explicit bounded deque, a global active counter, and
per-host token buckets. HTTP reads in chunks and enforces response bytes, total
timeout, and manual redirect count.

## Audit matrix

| Component | Current limit | Expected behavior | Vulnerability or gap identified | Test and correction |
|---|---:|---|---|---|
| Robots policy cache | Default 1,024 entries; TTL 3,600 seconds | Never exceed capacity; expire stale rules; evict least recently used | No code violation found; prior evidence lacked high volume and precise LRU/TTL assertions | Added 80-origin fill against capacity 7, hit promotion, expiry, failure/retry, and size checks; cache code unchanged |
| Robots single-flight | Default 128 in-flight origins; 64 waiters per origin; derived pending cap 8,320 | One fetch per origin; explicit rejection; cleanup after every outcome | Calls could accumulate while queued on the internal lock before the per-origin limits were checked | Added pre-await `pending_checks` cap; saturation, leader/waiter cancellation, distinct-origin concurrency and recovery tests |
| Admission controller | Default 16 active globally; 2 per host; 64 queue waiters; 2,048 host states; 30 second wait; derived pending cap 80 | Bound active and queued transport work; reject saturation; release on failures/cancellation | Calls could accumulate at the `Condition` lock before the explicit queue limit was checked | Added pre-await `pending_acquires` cap; held-condition saturation/recovery test |
| HTTP transport | Default timeout 8 seconds; response cap 500,000 bytes; 5 redirects | Bound each request and clean resources for every outcome | No live network or infrastructure evidence; synthetic transport coverage only | Existing timeout, DNS/HTTP failure, response cap, redirect, cancellation and cleanup tests pass |
| Internal references | Cache, inflight, pending calls, admission queue and host table are finite | No stale inflight entries, waiters or pending counters after completion | No residual state observed in tested failure/cancellation paths | Integrated saturation, timeout, eviction and recovery; assert counters after drain |
| Event-loop ownership | Defaults are stored on their loop; each policy records a weak reference to its first loop | No default cache/lock/Future/inflight sharing; explicit cross-loop use is rejected | No cross-process coordination | Same-loop reuse, custom-policy rejection on a second loop, independent defaults/cache, and closed-loop collection |

## Corrections implemented

The audit found that configured waiter limits did not bound tasks waiting to
acquire the internal `asyncio.Lock` or `asyncio.Condition` during lock
contention. Added synchronous pre-await counters so each method rejects before
joining those internal lock queues once its derived finite capacity is reached:

- `RobotsPolicy.max_pending_checks` is
  `max_inflight_origins * (max_waiters_per_origin + 1)`. With defaults this is
  8,320 simultaneous policy checks, covering each permitted origin leader and
  its permitted waiters.
- `AdmissionController.max_pending_acquires` is
  `global_limit + max_waiters`. With defaults this is 80 calls entering or
  waiting to acquire admission.

Counters decrement in `finally`; saturation raises the existing fail-closed
policy/admission error types. No new dependency or public configuration option
was added. These are per-instance, per-event-loop software limits. A caller can
still allocate task objects before those tasks begin executing the bounded
coroutine; upstream task creation and process memory belong to the caller/runtime
boundary.

`RobotsPolicy` now binds to its first running loop. Reuse on that loop is
supported; calls from any other loop raise `RuntimeError` before touching cache,
lock, Future, or inflight state. The owner loop is held by weak reference. The
default policy is stored as an attribute on its event loop instead of as a value
in a weak-key registry; this avoids a registry value retaining its key through
asyncio synchronization primitives. Separate loops therefore receive separate
default caches, and the registry creates no global strong reference to closed
loops.

Single-flight cleanup now runs in a narrowly scoped task shielded from repeated
cancellation of its caller. The caller loops until cleanup finishes, observes
the cleanup task result, and then re-raises the original `CancelledError`.
Leader cancellation removes the matching inflight entry and resolves its shared
Future with the retry marker; waiter cancellation decrements its waiter count
under the same lock. The cleanup task is never detached or left unobserved.

## Adversarial test evidence

Added thirteen Gate 1B.4 adversarial tests across
`backend/tests/intelligence/crawler/` in
`test_robots_resource_boundaries.py` and `test_admission.py`. They use synthetic
responses, events, held locks, controlled clocks, and bounded event-loop turns;
no network requests are made. Coverage includes:

- 80-origin cache fill against capacity 7, LRU promotion, TTL expiration,
  concurrent fills/eviction, failure not cached, and valid retry.
- Same-origin single-flight, distinct-origin simultaneous loads, inflight and
  waiter saturation, leader cancellation, repeated leader cancellation while
  cleanup is blocked on the policy lock, multiple waiter cancellation, and
  recovery without residual flights/counters. The repeated-cancel test verifies
  that the old shared Future is resolved, the waiter retries successfully, and
  no cleanup task remains pending.
- Internal lock/condition contention at their derived pending caps, with
  deterministic rejection before overflow and recovery after release.
- Integrated 12-origin saturation, gated synthetic responses, a synthetic
  timeout, cache eviction, and successful retry. Limits and counters are
  asserted during and after the run.
- Independent default robots policies across two sequential event loops,
  isolated cache objects, and collection of closed loops. Custom policy tests
  verify first-loop binding, same-loop reuse, and explicit cross-loop rejection.
  Existing tests also verify admission defaults are loop-local and explicit
  controllers reject cross-loop use.

The tests assert state invariants, including cache size, inflight count, waiter
count, active requests, queued requests, pending calls, and eventual slot
recovery. Concurrency states use events, held locks, or a bounded event-loop
predicate helper rather than arbitrary time sleeps.

## Validation results

Executed on the final implementation and tests:

| Command | Result |
|---|---|
| `pytest -q tests/intelligence/crawler` from `backend/` | **172 passed**; 0 failed, 0 skipped |
| `ruff check app/intelligence/crawler tests/intelligence/crawler` from `backend/` | **Passed**, all checks passed |
| `mypy app/intelligence/crawler` from `backend/` | **Passed**, no issues in 8 source files |

There were no ignored or skipped tests in the final crawler suite. Intermediate
runs exposed fixture expectation and import-order issues; these were corrected
before final validation. The results above are from the final code/test run.

## Limitations and unproven risks

- Synthetic tests do not prove actual CPU, RAM, network bandwidth, operating
  system scheduling, container isolation, egress policy, or Docker socket
  isolation. These remain SEC-OPS/infrastructure validation.
- Limits are local to one Python object/event loop and do not coordinate across
  worker processes. No distributed admission is claimed.
- The library bounds calls once they begin executing, but cannot prevent an
  upstream caller from creating dormant task objects.
- The robots parser is intentionally limited and is not asserted to be a full
  RFC 9309 implementation. Live DNS, TLS, third-party robots behavior, and
  network failure modes were not exercised.
- Integrated volume is bounded (24 synthetic host identities and 12 concurrent
  initial checks) and completes in seconds. It is not a memory profiling
  benchmark; no byte-level memory claim is made.
- Repeated cancellation during single-flight cleanup is covered by one
  deterministic blocked-lock scenario. An unbounded cancellation storm or
  event-loop shutdown during cleanup is outside the test evidence. Tests also
  cover waiter cancellation, transport cancellation, and admission
  cancellation/release.

## Approval criteria and decision

Criteria for this software gate: explicit finite cache, inflight, waiter,
admission, and pre-lock pending-call bounds; deterministic evidence for
saturation and recovery; no residual state in tested cancellation/failure
paths; all required tests and static checks pass; and no weakening of existing
crawler protections. The criteria are satisfied by the evidence above.

**Decision: PASS** for Gate 1B.4 software-level cache, concurrency, cancellation,
and resource-boundary behavior within the tested process/event-loop scope.
Infrastructure resource isolation and real network behavior remain unverified
and are outside this decision.
