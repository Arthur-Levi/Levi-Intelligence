# SEC/OPS Gate 1B.2-B.2.2 — Deadline and shutdown

## Decision

**INCONCLUSIVE for a mandatory hard deadline.** Cooperative async crawls have an absolute worker-boundary deadline, and the disposable worker container demonstrated bounded shutdown, cancellation-suppression detection, and PostgreSQL recovery. A synchronous operation that blocks the event loop can prevent the Python deadline callback and signal handler from running. The container hard stop applies only after a supervisor sends SIGTERM. A separate attempt execution process or an external per-attempt watchdog with a hard-kill boundary is required to guarantee a deadline against arbitrary blocking code. No production runtime was tested.

Branch: `feature/build-006a-secure-crawler`. Previous B.2.1 CI run `44826775356` was successful. The pre-existing `frontend/tsconfig.tsbuildinfo` was left untouched. No real website or production database was accessed.

## Lifecycle and timing contract

Previously, the worker claimed a job, polled the crawl every five seconds, renewed its 90-second PostgreSQL lease, and waited without a limit for active tasks after SIGTERM/SIGINT. A crawler that never returned could retain an execution slot indefinitely. Shutdown cancellation was also classified as user cancellation.

Now the worker records `time.monotonic()` immediately after the claim transaction returns. `CRAWLER_ATTEMPT_SECONDS` establishes a fixed deadline from that instant. The worker checks remaining time before and after the crawl and after the final cancellation check. The crawler runs as a child task; each wait is bounded by the lesser of five seconds and remaining budget. All DNS lookups, robots checks, redirects, retries within the injected crawler, and processing share that same child execution budget. None resets the clock. A task that exceeds it is cancelled. A child that still runs after the cancellation window causes the worker to exit 1, leaving its lease for PostgreSQL recovery. This process exit is fail closed for the worker slot, but it is not a guarantee against synchronous event-loop blocking.

| Setting | Default | Allowed range | Meaning |
| --- | ---: | ---: | --- |
| `CRAWLER_ATTEMPT_SECONDS` | 60 s | 1–3600 s | Absolute cooperative attempt budget from claim return. |
| `CRAWLER_CANCELLATION_SECONDS` | 5 s | 0.1–30 s | Child cancellation wait; cannot exceed shutdown cleanup. |
| `CRAWLER_POLL_SECONDS` | 1 s | 0.2–10 s | Idle/admission poll, not a deadline. |
| `CRAWLER_SHUTDOWN_GRACE_SECONDS` | 15 s | 0–300 s | Active-task grace from first signal. |
| `CRAWLER_SHUTDOWN_CLEANUP_SECONDS` | 5 s | 0.1–30 s | Task cancellation wait and separate engine-disposal wait. |
| `CRAWLER_CONTAINER_STOP_GRACE_SECONDS` | 30 s | 5–600 s | Compose SIGTERM-to-SIGKILL boundary. |

Startup rejects non-numeric, NaN, infinite, out-of-range, and inconsistent timing values. Grace plus two cleanup windows must fit inside the container stop grace with at least five seconds of margin. The default application maximum is 25 seconds; Compose allows 30 seconds. The 90-second PostgreSQL lease is separate and renewable every five seconds while the crawl is pending. A per-request aiohttp timeout remains eight seconds, with a four-second socket-connect ceiling; it can fail one operation but does not reset or replace the absolute attempt deadline. DNS uses aiohttp's resolver within the child task; there is no separate DNS timeout setting. The transport bounds redirects to five, response bytes to 500,000, and does not implement its own retries. PostgreSQL lease-expiration recovery applies exponential backoff capped at 60 seconds and at most five claim generations.

`attempt_timeout` is a stable, sanitized terminal failure code in the existing job and attempt ledger. It does not contain a URL, exception text, or credentials and does not create a retry. `unsafe_website`, `invalid_job`, and `crawler_error` retain their prior terminal behavior. A worker crash or unsafe-to-finalize shutdown leaves a running lease; recovery expires it and may retry if fewer than five generations have been claimed. No sixth attempt can be claimed. A per-operation transport timeout that is not the absolute attempt deadline remains `crawler_error` through the existing exception path.

All successful result changes remain in one PostgreSQL transaction. `persist_result` and `persist_failure` require the matching tenant, company, running status, lease token, and unexpired lease under row lock. The attempt ledger and source record are committed with the terminal state. The shared advisory transaction lock `681204117`, global active limit 8, per-tenant active limit 2, maximum attempts 5, admission idempotency, tenant checks, and API HTTP 202 contract were not changed. There is no exactly-once claim for external HTTP execution.

## Signal and forced termination behavior

The first SIGTERM or SIGINT sets a draining state and a monotonic shutdown start time. Repeated signals leave that time unchanged. The worker stops scheduling claims; a task that already claimed but sees draining before execution leaves the lease durable for recovery. Active tasks may finish during the fixed grace. Remaining tasks receive cancellation; worker shutdown does not manufacture a user cancellation. A task that cannot cancel within cleanup, or engine disposal that cannot finish within its own cleanup window, causes process exit 1. Docker Compose's 30-second stop grace is an external SIGKILL boundary when Python cannot cooperate. A forced kill leaves the database lease and attempt ledger to expire and recover; it cannot produce a completed result.

## Evidence and test matrix

Focused local execution boundary, worker shutdown, HTTP transport, company enrichment, and legacy route: **58 passed**. PostgreSQL 16 disposable recovery, fencing, deadline, fault/event, lifecycle, and smoke tests: **35 passed**. The first broad PostgreSQL run had one fixture collision because the manual container probe left a queued synthetic job; after deleting only that fixture, the identical suite passed 35/35. `ruff check app tests`, affected-module mypy, Compose config, and `git diff --check` passed. GitHub CI run **44917317835** on test checkpoint `feddb6a` completed **success**; the earlier deadline and shutdown CI runs **44916439044** and **44916555893** also succeeded. The documentation checkpoint CI is checked after its push. The R2.1 capacity matrix was not repeated.

| ID | Actual evidence | Result |
| --- | --- | --- |
| DL-01 | Normal injected crawl completed and persisted in boundary tests. | PASS, unit |
| DL-02 | Slow child cancelled at absolute budget; timeout failure and no result. | PASS, unit and PostgreSQL |
| DL-03 | Sequential stages shared one budget. | PASS, unit |
| DL-04 | Synthetic DNS/robots/redirect/retry-like stages shared budget; transport redirect regressions passed. No real redirect network probe. | PASS, unit boundary; network path unverified |
| DL-05 | NaN and inconsistent shutdown settings rejected; parser rejects invalid and non-finite values. | PASS, unit |
| DL-06 | Child cancellation observed; external worker cancellation propagated. | PASS, unit |
| DL-07 | Timed-out PostgreSQL attempt had no result or source record. | PASS, PostgreSQL |
| DL-08 | Timeout terminal at generation 1; existing five-generation exhaustion regression passed. | PASS, PostgreSQL |
| DL-09 | Expired lease rejected timeout finalization; stale result/failure regressions passed. | PASS, PostgreSQL |
| DL-10 | Actual worker container with cancellation-suppressing synthetic child exited 1 after deadline and cancellation window; ledger remained running then recovered. Synchronous event-loop block remains unbounded without external signal. | PARTIAL |
| SD-01 | Idle worker signal test exited and disposed engine. | PASS, unit |
| SD-02 | Active worker made no new claim after signal. | PASS, unit |
| SD-03 | Active synthetic job finished during grace. | PASS, unit |
| SD-04 | Over-grace task was cancelled without user-cancellation state. | PASS, unit |
| SD-05 | SIGTERM followed by SIGINT did not reopen admission. | PASS, unit |
| SD-06 | Actual worker SIGTERM exit 0 left running attempt, zero sources. | PASS, container/PostgreSQL |
| SD-07 | Expiring that lease queued it with `expired` ledger outcome. | PASS, container/PostgreSQL |
| SD-08 | Stale completion and failure after reassignment rejected. | PASS, PostgreSQL |
| SD-09 | Actual `python -m app.workers.crawler` container, synthetic mounted service, disposable PostgreSQL 16: SIGTERM while active exited 0; `crawler_worker_draining` logged. | PASS, container/PostgreSQL |
| SD-10 | Actual active worker SIGKILL exited 137 (not OOM); lease stayed running, then expired to queued with zero sources. | PASS, container/PostgreSQL |

The container probe used the built current-branch image and the actual worker entry point. The mounted test-only service replaced website traffic with a deterministic synthetic wait; the container used the B.2.1 UID, read-only root, capability, no-new-privileges, memory/swap, CPU, PID, and FD restrictions. It was started with equivalent `docker run` flags rather than `docker compose up`; Compose syntax was separately validated. The disposable database was PostgreSQL 16, on tmpfs in an isolated Docker network. The SIGTERM stop command completed in about four seconds with a ten-second command stop timeout; the declared application and Compose budgets are 25 and 30 seconds. The hard-stop probe explicitly ignored `CancelledError`, then logged `crawler_child_ignored_cancellation` and exited 1. After three synthetic attempts (graceful stop, SIGKILL, ignored cancellation), the ledger showed three `expired` outcomes, a queued job, and zero source records. Every probe used `timeout`, Docker's stop timeout, or both; the test fixture and containers were cleaned up.

## Limits, rollback, and next requirement

The deadline is cooperative at the Python event loop boundary. A synchronous blocker can prevent both the timer and Python signal handler from running; a DB operation or finalization transaction may also outlive the nominal attempt clock. Docker's stop grace bounds shutdown only after a signal arrives. Production signal delivery, scheduling, and hard-kill timing remain unverified. A separate attempt process with a parent watchdog and fenced IPC/result handoff, or an equivalent supervisor hard deadline per attempt, is required for a mandatory hard bound. This is a B.2.2 dependency to resolve before PASS; it does not authorize expansion into later database, network, secret, or supply-chain gates.

Rollback only the B.2.2 worker/timing/Compose deployment while preserving the PostgreSQL schema, queued jobs, running leases, and attempt ledger. Stop new crawler claims, let active leases expire, deploy the last accepted B.2.1 worker/runtime, and verify fenced recovery before resuming claims. Do not delete jobs or ledger rows. This checkpoint is **INCONCLUSIVE**, not a production approval; Gate 1B.2-B.3 has not begun.

## R1 resolution (2026-11-21)

The historical **INCONCLUSIVE** decision above remains the decision for the original cooperative-only implementation. [B.2.2-R1 hard-deadline evidence](SEC_OPS_GATE_1B2_B22_R1_HARD_DEADLINE.md) adds an independently supervised crawler child. In the tested disposable Linux/Docker runtime, the parent terminated synchronous blocking and SIGTERM-resistant children within the original attempt and cancellation budgets, stayed responsive, and preserved PostgreSQL fencing and recovery. The updated **B.2.2 acceptance decision is PASS for that supported tested runtime**, contingent on the R1 evidence and limits. Production approval remains separate; unsupported hosts, escaped process groups, uninterruptible kernel tasks, and PostgreSQL finalization outside the child execution deadline are not covered by that PASS. Gate B.3 has not begun.
