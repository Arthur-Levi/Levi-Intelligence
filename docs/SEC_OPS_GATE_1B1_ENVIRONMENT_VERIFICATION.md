# SEC-OPS Gate 1B.1-ENV — Disposable PostgreSQL Runtime Verification

**Repository:** `Arthur-Levi/Levi-Intelligence`  
**Branch:** `feature/build-006a-secure-crawler`  
**Date:** 2026-11-20  
**Decision:** **BLOCKED — DOCKER ACCESS DENIED**

## Docker access

The checked-out branch is the required branch. The Docker client is installed
(29.9.2-2) and selects context `default`, endpoint
`unix:///var/run/docker.sock`. The current process is `uid=1000(codespace)` with
groups `codespace` and `nogroup`. The socket is owned by `nobody:nogroup` with
mode `srw-rw----`.

Both `docker version` and `docker info` returned:

```text
permission denied while trying to connect to the docker API at unix:///var/run/docker.sock
```

The server section of `docker version` was unavailable. No `sudo`, socket
permission change, daemon restart, or alternate runtime was attempted.

## Static configuration findings

The Compose declaration for `postgres-test` uses image `postgres:16-alpine`,
the `test` profile, a test-specific database name and user, and publishes
container port 5432 on host `127.0.0.1:55432`. Its PostgreSQL data directory is
declared as `tmpfs`; no named or bind data volume is declared for this service.
The development `postgres` service is separate in the Compose file and declares
a named persistent volume at `/var/lib/postgresql/data`, with a different
loopback published port. `postgres-test` has no declared dependency on the
development database, discovery worker, or crawler worker.

The Compose declaration has no explicit network override for `postgres-test`;
Compose therefore declares the project default network. The static file does
not declare an external database endpoint for `postgres-test`, but it cannot
prove effective runtime connectivity or the absence of additional runtime
configuration.

The backend's database setting is read by `backend/app/db.py` from application
settings. Alembic's `backend/alembic/env.py` uses that setting. The crawler
worker separately reads `CRAWLER_DATABASE_URL`. The opt-in tenant integration
test reads `TEST_DATABASE_URL`. This audit recorded variable names only and did
not inspect environment values.

| Isolation requirement | Static declaration | Runtime status |
|---|---|---|
| Dedicated test database and user | Declared | Not verified through a live connection |
| Ephemeral storage | `tmpfs` declared | Effective mount not verified |
| No shared named data volume | None declared for `postgres-test` | Effective mounts not verified |
| No development data-directory mount | None declared for `postgres-test` | Runtime mounts not inspectable |
| No connection to preserved database | No such endpoint declared in the service | Effective network/connections not verified |
| Local-only host exposure | `127.0.0.1:55432` declared | Actual port binding not inspectable |

## Runtime verification

Runtime verification stopped at the Docker API permission failure. No
container was started or inspected. No database connection or identity query
was made. Container ID, effective image digest, effective mount type, actual
port binding, server version, database identity, and connection destination
are therefore unavailable.

No migration or lifecycle test was run. No preserved database was connected to
or modified.

## Commands and results

Commands were run from `/workspaces/Levi-Intelligence` unless noted:

| Command | Result |
|---|---|
| `id` | UID 1000 `codespace`; primary group `codespace`; supplementary group `nogroup`. |
| `groups` | `codespace nogroup`. |
| `ls -l /var/run/docker.sock` | Socket is `srw-rw----`, owner `nobody:nogroup`. |
| `docker context show` | `default`. |
| `docker context ls` | `default` targets `unix:///var/run/docker.sock`. |
| `docker version` | Client available; server access denied with the exact error above. |
| `docker info` | Server access denied with the exact error above. |
| `git status --short --branch` | Clean at audit start on the required branch, synchronized with origin. |
| `git branch --show-current` | `feature/build-006a-secure-crawler`. |
| Static Compose inspection | `postgres-test` declares tmpfs and loopback publication; development `postgres` declares a separate named volume. No runtime claims inferred. |
| Backend configuration inspection | Settings/Alembic use the configured database setting; crawler worker and integration test use separate environment variable names. Values were not included in this report. |

## Assessment

The static Compose declarations are consistent with an intended disposable
PostgreSQL test service. They do not establish the effective runtime storage,
network, published port, server identity, or connection destination. Since the
Docker daemon is inaccessible, the required positive runtime proof cannot be
completed in this environment. A human or environment administrator must
provide authorized Docker API access for read-only inspection and controlled
startup of only `postgres-test`; no permission bypass should be used.

This is an environment verification result only. It does not claim acceptance
of SEC-OPS Gate 1B.1 or infrastructure Gate 1.

**BLOCKED — DOCKER ACCESS DENIED**
