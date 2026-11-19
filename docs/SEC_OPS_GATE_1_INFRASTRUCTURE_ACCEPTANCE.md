# SEC-OPS Gate 1 — Infrastructure Isolation, Runtime Enforcement & Abuse Resistance

**Repository:** `Arthur-Levi/Levi-Intelligence`
**Required branch:** `feature/build-006a-secure-crawler`
**Inspected commit:** `b78d5c4af9bdd0864229755e246ab953ee007047`
**Decision:** **NO-GO** for production infrastructure acceptance

## 1. Scope

This gate inspected repository deployment configuration, crawler and worker
execution paths, CI, the available Codespaces process/container environment,
and safe existing tests. No container was started, stopped, or inspected through
the Docker daemon. No database, persistent volume, host firewall, network
configuration, or production resource was modified. No live network probes were
sent.

The result distinguishes repository declarations from observed runtime
properties. A configuration declaration is not evidence that a deployed service
uses it or that the host enforces it.

## 2. Threat model

The protected assets are the Codespaces/deployment host, internal networks,
databases and credentials, Docker/container control plane, other tenants and
services, worker availability, and bounded CPU, memory, process, socket, queue,
and temporary-storage use. The crawler handles hostile URLs, DNS answers,
redirects, response bodies, and caller input.

The accepted Build 006A crawler controls are application-level controls. A
compromised process, library, or runtime may bypass them unless egress, identity,
container, and host resource boundaries enforce the same restrictions outside
the Python process.

## 3. Infrastructure inventory

| Area | Repository evidence | Finding |
|---|---|---|
| Container build | `backend/Dockerfile` | Python 3.12 slim image, installs the application, copies source, default command starts Uvicorn. No `USER`, read-only root setting, capability, privilege, namespace, seccomp/AppArmor, or resource policy is declared. The image therefore defaults to root unless an external runtime overrides its user. |
| Compose services | `docker-compose.yml` | `postgres`, `redis`, profiled `discovery-worker`, and profiled `postgres-test`. No API or dedicated crawler service is declared. The Compose default network is used; no egress rules or service-specific network separation are declared. |
| Database | `docker-compose.yml` | PostgreSQL port is published on `127.0.0.1:5432`; persistent named volume `levi_postgres_data` is declared. This is a local Compose declaration, not proof of an actual deployment binding. The volume was not accessed. |
| Redis | `docker-compose.yml` | Port is published on `127.0.0.1:6379`; named persistent volume is declared. Redis is not the discovery queue in the current implementation. |
| Test database | `docker-compose.yml`, `backend/tests/integration/README.md` | `postgres-test` is opt-in under profile `test`, uses tmpfs for its data directory and binds loopback port 55432. It was not started. |
| Worker | `backend/app/workers/discovery.py`, Compose profile `workers` | A database-polling discovery worker claims `prospecting_runs`. It is not a crawler-only worker and runs in the default Compose network with PostgreSQL/Redis. |
| Crawler call site | `backend/app/api/v1/endpoints/prospecting.py`, `backend/app/services/company_enrichment.py` | Website crawling is called synchronously by the API enrichment endpoint. There is no dedicated crawler process or service in the repository Compose configuration. |
| Secrets/configuration | `.env.example`, Compose worker `env_file` | Local development defaults and an optional `.env` file are used. The worker receives the `.env` values and a database URL. No production secret manager, narrowly scoped crawler identity, or secret isolation policy is evidenced. |
| CI | `.github/workflows/ci` | Runs backend Ruff and pytest and frontend build on GitHub-hosted runners. No container image security/runtime policy, deployment network policy, resource enforcement, or runtime inspection is configured here. |
| Additional deployment assets | Repository file inventory | No Kubernetes manifests, Terraform, systemd service, dedicated crawler Dockerfile, or other deployment/runtime policy files were found. |

`docker compose config --services` parsed the default service set as `postgres`
and `redis`; with profile `workers` it additionally listed `discovery-worker`;
with profile `test` it additionally listed `postgres-test`. This verifies Compose
parsing only, not service creation or runtime enforcement.

## 4. Runtime environment evidence

Read-only inspection of the current Codespaces command environment found:

| Evidence | Observation | Limitation |
|---|---|---|
| Identity | UID/GID 1000 `codespace`; supplementary groups 1000 and 65534 (`nogroup`); not root | Describes this agent process only, not the eventual crawler deployment identity. |
| Capabilities | `CapEff=0`, `CapBnd=0` | Does not establish the host or future service capability set. |
| Privilege escalation | `NoNewPrivs=1` | Current process only. |
| Seccomp | Mode 2, one filter | Filter contents and coverage are unavailable; no application-specific profile is proven. |
| LSM | `/proc/self/attr/current` reports `unconfined`; SELinux path absent | AppArmor policy directory exists, but the current process is reported unconfined. No crawler-specific MAC policy is established. |
| Namespaces | Current process and PID 1 have the same cgroup, IPC, mount, network, PID, user, and UTS namespace IDs | This shows no further namespace separation from this container's PID 1. It does not compare the Codespaces container with its host. |
| Filesystem | Current root mount is read-only; workspace is a writable bind mount; `/tmp` is writable | Does not prove isolation from all host or sibling-container paths. No production crawler filesystem boundary was inspected. |
| Cgroups | Visible cgroup path `/init`; `cpu.max=max 100000`, `memory.max=max`, `memory.high=max`, and `memory.swap.max=max`; visible `pids.max=9522`; cgroup filesystem is read-only | CPU/memory limits are not bounded at the visible process cgroup. Outer host constraints are not observable here. The PID value is not a crawler service limit. |
| Docker CLI/daemon | Docker CLI 29.9.2 is installed. `/var/run/docker.sock` and `/run/docker.sock` appear as mode 0660, owner `nobody:nogroup`; current process belongs to `nogroup`. `docker version` reports permission denied connecting to the API. | Daemon access was denied, so running containers, daemon configuration, and applied container policies could not be inspected. The socket mount and group relationship are a Codespaces-specific concern; they do not prove it is mounted in a crawler deployment. |
| Network | A network namespace exists for this container | No host egress rules, Docker network policy, route restrictions, DNS restrictions, or production network enforcement could be inspected. No connectivity tests were made. |

The runtime checks are evidence about this Codespaces process/container only.
They are not evidence that production will use the same settings.

## 5. Network isolation findings

**Status: NOT VERIFIED; no repository-level egress enforcement is implemented.**

- Compose does not declare a dedicated crawler network, deny-by-default egress,
  DNS policy, firewall rule, or explicit external destination allowlist.
- If the Compose `discovery-worker` is deployed, its default network includes
  access to the PostgreSQL and Redis services. The website crawler currently runs
  in the API process, so isolating only the discovery worker would not isolate
  crawler traffic.
- The crawler's `DNSGuard`, URL checks, and pinning are software controls. They
  do not block routing to private, link-local, loopback, metadata, Docker API, or
  other internal destinations if process code or the runtime bypasses those
  checks.
- The Codespaces container has a separate visible network namespace from an
  unknown host namespace, but actual egress restrictions cannot be inferred from
  that fact. No real network attempt or scan was performed.
- The Docker socket is not mounted by the repository Compose worker definition.
  The Codespaces environment itself exposes a socket path with group-readable and
  group-writable mode to a supplementary group of this process, while Docker API
  access is denied. This must not be generalized to production; the socket should
  not be made available to crawler processes.

No evidence establishes protection against loopback, private IPv4/IPv6,
link-local, cloud metadata, internal databases, control planes, Docker endpoints,
or unauthorized containers at the network layer.

## 6. Container and process isolation findings

**Status: NOT IMPLEMENTED for a dedicated crawler; deployment enforcement NOT VERIFIED.**

- There is no crawler service/container definition. The default Dockerfile
  declares no non-root user and no container security settings.
- The Compose discovery worker runs the application image with no `read_only`,
  `cap_drop`, `security_opt`, `no-new-privileges`, `user`, `tmpfs`, mount
  restrictions, PID limit, or dedicated namespace/network settings.
- The Compose worker has no host bind mounts or Docker socket mount in the file.
  It receives the `.env` file when present and a PostgreSQL credential in its
  environment. Its permissions inside a running container were not inspected.
- Root filesystem, writable paths, mount propagation, host path access, PID/IPC/
  network namespace boundaries, seccomp profile, and AppArmor profile for a
  deployed worker are not proven by this repository.
- In this Codespaces process, UID 1000, no capabilities, `NoNewPrivs`, seccomp,
  and read-only root are observed, but AppArmor reports `unconfined`, cgroup
  resource ceilings are largely unlimited, and Docker daemon inspection is
  blocked. This is not an acceptable substitute for deployment evidence.

## 7. Resource enforcement findings

| Control | Classification | Evidence |
|---|---|---|
| URL/redirect/response bounds | IMPLEMENTED AND VERIFIED at software level | Crawler tests cover URL/DNS policy, five-hop default redirect limit, 500,000-byte default response cap, and request timeouts. |
| Request concurrency/rate | IMPLEMENTED AND VERIFIED at software level | Admission tests cover in-process global/per-origin slots, bounded waiters, rate controls, cancellation and recovery. State is per event loop/process, not distributed. |
| Robots cache/inflight work | IMPLEMENTED AND VERIFIED at software level | Finite cache, in-flight, waiter, and pending-call bounds have deterministic tests. |
| Task/input HTTP body limits | PARTIALLY IMPLEMENTED | Query and idempotency key have schema limits; filters are an unconstrained dictionary. No infrastructure request-body or task submission rate/size budget is declared. |
| Database queue size | NOT IMPLEMENTED | `prospecting_runs` is a durable database-backed queue, but no global or per-tenant queued-job ceiling, retention cap, or admission quota was found. Distinct idempotency keys can enqueue distinct jobs. |
| CPU/memory/swap | NOT VERIFIED / NOT IMPLEMENTED in repository deployment | No Compose/deployment limits. Current visible Codespaces cgroup CPU, memory, and swap maxima are `max`; outer enforcement is unknown. |
| PID/task count | NOT IMPLEMENTED in repository deployment | No Compose `pids_limit` or deployment limit. Current visible cgroup value is 9522 and is not a crawler-specific bound. |
| File descriptors/sockets | NOT IMPLEMENTED in repository deployment | No service `ulimits` or socket cap is declared. Host/process effective limits were not asserted as crawler limits. |
| Temporary storage | NOT IMPLEMENTED for worker | No worker tmpfs size or writable-layer quota is declared. Test PostgreSQL uses tmpfs, but it is not the crawler runtime. |
| Worker/provider duration | PARTIALLY IMPLEMENTED | Apify HTTP calls have a 180-second timeout and polling is bounded to 60 intervals; there is no whole-job lease/timeout or process-level kill boundary. Crawler requests have software timeouts only. |

Application-level response and concurrency controls are not operating-system
resource enforcement.

## 8. Queue and worker lifecycle findings

The repository does contain a PostgreSQL-backed discovery queue, but it is not a
crawler queue/service boundary.

- Jobs are inserted by the API and polled by `app.workers.discovery` every
  configured interval (default two seconds). `_claim_one()` uses a transaction,
  `FOR UPDATE SKIP LOCKED`, and changes one `QUEUED` row to `RUNNING`.
- Tenant-scoped idempotency prevents a repeated matching key from creating the
  same request twice. It does not bound the number of different jobs or guarantee
  fairness among tenants.
- Failure backoff and maximum attempts are present. Maximum attempts defaults to
  three and is configurable up to ten.
- Cancellation of queued work is immediate. Running work records a request, but
  provider work is not force-cancelled; the status is checked after provider
  completion. `_is_cancelled()` exists but is not used to interrupt active work.
- There is no worker lease, heartbeat, stale-`RUNNING` recovery, or reaper. A
  process crash or `CancelledError` after a job is claimed can leave a row
  permanently `RUNNING`, because future claims select only `QUEUED` rows.
- The worker loop has no explicit graceful-shutdown protocol. It processes one
  job at a time per process, but additional processes can run; crawler admission
  limits are not shared between them.
- No queue capacity, tenant quota, per-tenant concurrency/fairness, dead-letter
  policy, task payload size limit, or queue age bound was found.
- There are no dedicated worker/queue lifecycle tests in `backend/tests`. The
  discovery enrichment tests do not exercise database claiming, crash recovery,
  cancellation during provider execution, queue saturation, or tenant contention.
- No worker tests were run against a database. The integration test requiring a
  dedicated disposable PostgreSQL URL was allowed to skip; no test service was
  started and no preserved database was accessed.

## 9. Abuse and failure resistance results

Previously accepted crawler tests provide synthetic evidence for oversized
responses, excessive redirects, DNS/address rejection, concurrency saturation,
slow/timeout behavior, repeated cancellation, and crawler resource cleanup within
one process. These do not establish egress isolation or OS resource enforcement.

No controlled stress workload was run because no disposable bounded runtime is
available. In particular, queue submission saturation, worker crash and restart,
graceful shutdown, provider cancellation, retry storms, cross-tenant queue
contention, process memory/CPU ceilings, PID limits, file descriptors, and
temporary storage remain untested. No claim of worker lifecycle or
cross-tenant resource isolation is made.

## 10. Controls implemented and classifications

| Control | Classification |
|---|---|
| Crawler URL/DNS/robots/redirect safeguards | IMPLEMENTED AND VERIFIED at software level by Gate 1B.5 tests |
| In-process crawler response, timeout, concurrency, cache, and cancellation bounds | IMPLEMENTED AND VERIFIED at software level; not distributed or OS-enforced |
| PostgreSQL durable discovery queue, row-lock claim, idempotency, retry/backoff | IMPLEMENTED BUT NOT VERIFIED against PostgreSQL in this environment |
| Compose loopback-only host bindings for local PostgreSQL/Redis | IMPLEMENTED IN REPOSITORY CONFIGURATION; runtime binding NOT VERIFIED |
| Dedicated crawler process/container | NOT IMPLEMENTED |
| Host-enforced crawler egress policy | NOT IMPLEMENTED in repository; runtime NOT VERIFIED |
| Non-root crawler identity, capability removal, read-only root, MAC profile | NOT IMPLEMENTED in repository deployment configuration |
| CPU/memory/PID/FD/socket/tmp limits for crawler | NOT IMPLEMENTED in repository deployment configuration |
| Bounded queue and per-tenant job quotas/fair scheduling | NOT IMPLEMENTED |
| Worker leases, heartbeat, stale-job recovery, graceful shutdown | NOT IMPLEMENTED |
| Worker/queue synthetic lifecycle suite | NOT IMPLEMENTED |
| Codespaces Docker daemon and actual container inventory | BLOCKED BY ENVIRONMENT (Docker API permission denied) |
| Production runtime/network enforcement evidence | NOT VERIFIED; no deployment environment or operator evidence supplied |

## 11. Controls not verified and environmental limitations

- Docker daemon and running containers could not be inspected because access to
  `/var/run/docker.sock` returned permission denied. No Docker runtime action was
  attempted.
- No production deployment target, network policy, firewall rules, orchestrator
  manifests, cgroup hierarchy, service identity, or host operator evidence was
  supplied.
- Codespaces' outer host, network path, container runtime configuration, and
  cgroup enforcement outside the visible namespace are not inspectable here.
- No disposable resource-limited crawler runtime or PostgreSQL test service was
  started. No preserved database or volume was touched.
- Static Compose parsing does not show which services are running or prove
  effective bindings, networks, resource limits, or security profiles.

## 12. Required remediation and safe implementation plan

Do not deploy the crawler as production-ready from this evidence. Before a new
implementation gate, select and document the actual deployment target and who
owns its host/network policy. Then implement the minimum compatible boundary:

1. Move website crawling out of the API request process into a dedicated crawler
   execution service. Give it only the job/result interface and credentials it
   needs; avoid general application database credentials if a narrow broker or
   result API can provide the required access.
2. Attach crawler execution to an enforced deny-by-default egress boundary that
   blocks loopback, private/reserved IPv4 and IPv6, link-local, metadata, internal
   service/control-plane ranges, and Docker management endpoints. Permit only
   the intended DNS path and authorized external web egress. Provide synthetic
   policy tests and runtime policy inspection evidence. Application DNSGuard
   remains defense in depth.
3. Run the crawler as a dedicated non-root UID/GID with no added capabilities,
   no privilege escalation, read-only root, a bounded writable temporary area,
   no host mounts, no Docker socket, dedicated namespaces, and an enforced
   seccomp/AppArmor-equivalent profile. Verify the effective running settings,
   not only Compose declarations.
4. Apply runtime CPU, memory, swap, PID, file descriptor, socket, and writable
   storage limits with conservative headroom. Demonstrate enforcement using
   bounded disposable workloads that cannot exhaust the host.
5. Bound queued work and request payloads globally and per tenant. Add fair
   admission so one tenant cannot starve others. Retain idempotency while
   specifying deduplication semantics for distinct keys.
6. Add job leases/heartbeats and stale-claim recovery, cooperative cancellation
   checks during long provider work, explicit graceful shutdown, and a
   dead-letter/terminal retry policy. Test cancellation, crash/restart, duplicate
   submission, queue saturation, retry bounds, and recovery with a synthetic
   database or disposable test database only.
7. Add deployment acceptance that inspects effective container identity,
   capabilities, namespaces, mounts, cgroup limits, and network policy in the
   chosen runtime. Keep production approval separate from repository unit-test
   success.

This gate intentionally makes no Compose or host changes: the repository does
not establish the deployment target or the API/crawler service boundary needed
to apply and verify the controls without risking compatibility or creating a
false enforcement claim.

## 13. Validation evidence

Commands ran from `backend/`, except Compose parsing and Git checks:

| Command | Result |
|---|---|
| `pytest -q tests/intelligence/crawler` | **177 passed**, 0 failed, 0 skipped (2.28s) |
| `pytest -q tests/intelligence/crawler tests/test_discovery_enrichment.py` | **186 passed**, 0 failed, 0 skipped (2.97s) |
| `pytest -q tests/integration` | **1 skipped** because the dedicated `TEST_DATABASE_URL` was unavailable; 0 passed, 0 failed. No database was started or accessed. |
| `ruff check app/intelligence/crawler tests/intelligence/crawler` | **Passed**, all checks passed |
| `mypy app/intelligence/crawler app/workers/discovery.py` | **Passed**, no issues in 9 source files |
| `ruff check app/intelligence/crawler tests/intelligence/crawler app/workers/discovery.py` | **Failed with 12 existing findings** in unmodified `app/workers/discovery.py` (import ordering and line length); crawler-only Ruff target passes. |
| `docker compose config --services` and profile variants | **Passed parsing**; listed services by profile. This is static configuration evidence only. |
| Docker daemon inspection | **Blocked**; Docker CLI returned permission denied on the daemon socket. |

No worker lifecycle tests exist in the discovered test tree, so none are claimed
as executed. No resource-exhaustion or network access test was run.

## 14. Acceptance decision

**Decision: NO-GO** for SEC-OPS Gate 1 production infrastructure acceptance.

The repository has no dedicated crawler runtime, no enforced egress policy,
no crawler OS resource limits, and no container hardening declarations. The
existing discovery queue is unbounded and lacks crash recovery for claimed
`RUNNING` jobs. The only available runtime is a Codespaces container whose Docker
daemon and host enforcement cannot be inspected; visible CPU and memory cgroup
ceilings are unlimited. These are missing or unverified infrastructure controls,
not failures of the accepted software-level crawler gate. Repository-level test
success does not authorize production deployment.
