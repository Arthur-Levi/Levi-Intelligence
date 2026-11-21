# SEC/OPS Gate 1B.2-B.2.2-R1 — independent attempt executor

## Architecture and decision

**PASS for the tested disposable Linux/Docker runtime.** The independent child process resolves the B.2.2 synchronous-blocking gap in that supported runtime. This does not approve production deployment or establish a general security sandbox. The original B.2.2 INCONCLUSIVE decision remains historical; its supported-runtime acceptance is updated in that report after this R1 evidence.

The existing PostgreSQL worker remains the supervisor. It claims jobs, starts a fixed monotonic attempt clock at claim return, heartbeats the renewable 90-second lease, observes user cancellation and shutdown, validates child output, and performs every fenced database transition. The child receives no repository, session, lease token, tenant identity, or database URL. It validates and crawls one URL and returns one result or a stable error code.

The supervisor starts a fresh Python interpreter with `subprocess.Popen`, closed inherited file descriptors, a minimal explicit environment and a new process session. Synchronous `Popen` returns an owned handle before the first await, eliminating the `create_subprocess_exec` cancellation window. A deterministic cancellation immediately after spawn reaped the child. The child environment contains only `PATH`, `LANG`, `PYTHONDONTWRITEBYTECODE` and `PYTHONUNBUFFERED`; it receives no database URL, test secret marker, repository object, tenant authorization token or lease token. PostgreSQL integration verified no inherited connection socket. The child has no database-state transition code in its entry point. Its same-container UID and network access are **not** a security sandbox or egress policy.

The supervisor writes one UTF-8 JSON request of at most **1,024 bytes** to nonblocking stdin, then closes stdin. It contains exactly `version: 1`, the acquired `attempt_id`, and `url`. The child writes one newline-terminated UTF-8 JSON response of at most **8,192 bytes** to stdout, then exits. A response has exactly `version`, `attempt_id`, `kind`, and either `result` or `code`. The parent requires the acquired attempt ID, strict integer protocol version, exact fields, unique JSON keys, finite JSON values, exit status zero, and a valid `CrawlerJobResult`. Child error codes are restricted to `unsafe_website` and `crawler_error`; malformed, partial, oversized or late messages fail closed. No Python object deserialization, raw exception text or unbounded pipe buffering crosses IPC. The child sends no durable-state instruction.

The supervisor's absolute monotonic attempt deadline starts immediately after claim return and covers startup, input write, DNS/robots/HTTP/redirect/retry/extraction work, output reading, child exit, IPC decoding and result acceptance. No stage resets it. At expiry it rejects success, sends SIGTERM to the child's process group, escalates to SIGKILL after the configured cancellation window, verifies no live group member through Linux `/proc`, and reaps the direct child. It reserves the direct child's PID until group signaling completes, avoiding a stale process-group identifier. A child that cannot be stopped is not finalized; the worker exits and the durable lease recovers. Normal success also cleans up any descendant left in the child's process group.

The execution deadline does **not** include PostgreSQL finalization. The 90-second renewable lease and row-locked ownership check govern `persist_result` and `persist_failure`. A stale lease cannot commit a result or timeout failure. An unresponsive PostgreSQL operation may delay finalization or shutdown; this is outside the child execution hard bound and remains a database/runtime operational risk. The B.2.2 worker shutdown grace (15 seconds), cleanup windows (5 seconds each) and Compose stop grace (30 seconds) remain unchanged by R1.

The supported hard-stop boundary is a disposable Linux container using a dedicated child process group and readable `/proc`. A descendant that deliberately escapes that group, an uninterruptible kernel task, a pathological OS spawn, or an unsupported host is outside this guarantee. `Popen` is synchronous and its trusted interpreter startup can briefly occupy the parent event loop; the parent checks the original deadline immediately after it returns. The B.2.1 UID/GID 10001, read-only root, dropped capabilities, no-new-privileges, 512-MiB memory with zero extra swap, one CPU, 64 PIDs and 1,024 FDs were not increased. The configured worker concurrency remains two. During an active blocked attempt, Docker observed **three PIDs** and **85.96 MiB** cgroup memory; `docker top` showed the parent and one child with separate process groups. This measures one attempt, not a proven peak at concurrency two.

## Evidence and mandatory matrix

Process-level tests use a synthetic child script and external pytest timeouts. The actual container used `python -m app.workers.crawler` from the R1 image with a mounted test-only synthetic service; no real website was contacted. Docker run flags matched the B.2.1 runtime ceilings, and Compose config validated separately. The disposable database was PostgreSQL 16 on tmpfs in an isolated Docker network. Test fixtures were removed before the full integration suite so unrelated tests could not claim them.

| ID | Evidence | Result |
| --- | --- | --- |
| HD-01 | Normal child result validated and reaped; actual worker container committed one completed attempt and source record. | PASS, process/container/PostgreSQL |
| HD-02 | Synchronous `time.sleep` loop killed at parent deadline; actual container parent stayed alive and recorded `attempt_timeout`. | PASS, process/container/PostgreSQL |
| HD-03 | CPU infinite loop terminated and direct child reaped. | PASS, process |
| HD-04 | SIGTERM-ignoring child required SIGKILL; actual container parent stayed alive and recorded timeout. | PASS, process/container/PostgreSQL |
| HD-05 | Child crash and nonzero exit after a success-looking reply could not produce success. | PASS, process |
| HD-06 | Oversized, malformed, partial, duplicate-key, extra-field, bad-version, non-finite and invalid-schema replies rejected. | PASS, process |
| HD-07 | Reply after original deadline rejected. | PASS, process |
| HD-08 | Existing PostgreSQL stale-token result and failure tests passed after reassignment. | PASS, PostgreSQL |
| HD-09 | Direct child and same-group descendants stopped, including on successful reply; observed child PIDs absent after container cleanup. | PASS, process/container |
| HD-10 | Existing five-generation exhaustion and no-sixth-attempt tests passed. | PASS, PostgreSQL |
| HD-11 | Unit tests stopped claims; actual active parent SIGTERM exited 0 within stop budget and left durable running attempt. | PASS, unit/container/PostgreSQL |
| HD-12 | Actual active parent SIGKILL exited 137, child disappeared, lease later queued with `expired` ledger outcome and zero sources. | PASS, container/PostgreSQL |
| HD-13 | Actual blocked child was visible in `docker top`; parent renewed lease, then child disappeared at 8-second attempt deadline while parent remained running. | PASS, container/PostgreSQL |
| HD-14 | Focused process/deadline/shutdown/crawler/legacy tests **83 passed**; full PostgreSQL integration **54 passed**. | PASS, unit/process/PostgreSQL |
| HD-15 | Synthetic environment markers and inheritable FD excluded; PostgreSQL integration child observed no database URL or connection socket. Request carries URL and attempt ID only. | PASS, process/PostgreSQL |

The first normal container probe failed safely with `crawler_error` because its synthetic readiness marker was printed on stdout, violating the strict IPC protocol. The fixture was corrected to use stderr; the repeated actual worker probe completed successfully. No production data or website was involved. The blocked container's PostgreSQL attempt showed one lease renewal, then `attempt_timeout` with no source. The SIGTERM probe left one running attempt with zero sources; forced lease expiry queued it. A second worker was SIGKILLed while active; its lease likewise expired to queued at generation two, with two `expired` ledger outcomes and zero sources. The process-level child environment test ran while a PostgreSQL connection existed in the parent.

Local validation: `ruff check app tests`, affected-module mypy, `docker compose --profile workers config --quiet`, and `git diff --check` passed. The R2.1 capacity matrix was not repeated. Final GitHub CI status is checked on the final documentation SHA.

## Residual risks and rollback

The 1,024-byte input limit fits a 500-character ASCII URL and the 8,192-byte output limit fits the tested ten-email/ten-phone result. A valid URL with heavy Unicode expansion or a schema-valid but unusually large result can exceed IPC limits and fails closed; it is never truncated. The same-container child is still able to access the container's network namespace; Gate D owns egress policy. Linux process-group cleanup does not cover a deliberately escaped descendant, and uninterruptible kernel tasks are not guaranteed to stop in real time. Production image, signal timing and resource peaks remain unverified.

Rollback the R1 worker image and process-executor code through a controlled deployment while preserving queued jobs, running leases and the attempt ledger. Stop new claims, let owned leases expire or finish, deploy the prior B.2.2 worker, and verify fenced recovery. Do not delete or rewrite durable job state. No merge to main or B.3 work is part of this decision.
