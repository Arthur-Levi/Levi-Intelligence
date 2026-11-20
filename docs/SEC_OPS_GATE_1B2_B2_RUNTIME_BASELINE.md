# SEC/OPS Gate 1B.2-B.2.1 — Effective crawler worker runtime baseline

## Scope and decision

**PASS for the disposable Docker Engine environment tested on 2026-11-20.** This verifies the Compose crawler service's operating-system boundaries with a synthetic process, plus PostgreSQL 16 lease-recovery regression. It does **not** approve a production deployment or prove that a real website request completes inside this container. B.2.2 still owns the absolute job deadline, bounded worker shutdown, queue-recovery saturation and DB pool ceiling.

The branch was `feature/build-006a-secure-crawler`, aligned with `origin` at `7869d75` before this checkpoint. The sole pre-existing untracked file, `frontend/tsconfig.tsbuildinfo`, was left untouched. Previous contract: [B.1 acceptance](SEC_OPS_GATE_1B2_B1_LEGACY_ROUTE_MIGRATION.md); broader requirements: [1B.2 roadmap](SEC_OPS_GATE_1B2_ROADMAP.md). This checkpoint changes only `docker-compose.yml` and this report; no worker, API, schema, PostgreSQL-role or egress-policy code changed.

## Runtime and method

Docker Engine **29.9.2-2**, Compose **5.6.1**, containerd **2.4.2-4**, runc **1.5.2-15**, host reported as Ubuntu **24.04.5 LTS (containerized)**, cgroup v2, overlayfs. `backend/Dockerfile` uses `python:3.12-slim` (base digest `sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b`) and copies the application. The initial Compose-built crawler image ID was `sha256:0b2f36f0021ae56f2bddcbf8223ac32837f61c4aaa5fc26fed29c5925bd0f413`. The disposable service used the actual crawler-worker Compose security settings with its command overridden to a bounded Python probe; it did not connect to the application database or a website. The later `compose up` run used an isolated project (`levi-b21`) to verify `restart: unless-stopped` after synthetic process exit.

The Docker socket was denied in the ordinary workspace sandbox; `docker version` succeeded with approved daemon access. `docker compose --profile workers config --quiet` succeeded with a synthetic `CRAWLER_DATABASE_URL`. No production credential, customer data or external website was used.

## Declared, observed and tested controls

| Boundary | Compose declaration | Effective observation and behavior | Status |
| --- | --- | --- | --- |
| Identity | `user: "10001:10001"` | `os.getuid()/getgid()` both 10001; `os.setuid(0)` returned EPERM (1). | PASS |
| Privileges | `cap_drop: [ALL]`, `no-new-privileges:true`, no privileged mode | `docker inspect`: `Privileged=false`; `/proc/self/status`: CapInh/Prm/Eff/Bnd/Amb all zero, `NoNewPrivs: 1`. | PASS |
| Root and temporary filesystem | `read_only: true`; `/tmp` tmpfs 16 MiB, `rw,noexec,nosuid` | `/app` write returned EROFS (30); `/tmp` write/read/delete succeeded; `/proc/self/mountinfo` showed 16,384 KiB tmpfs with `nosuid,nodev,noexec`; `Mounts=[]`. | PASS |
| Host mounts | None declared | `docker inspect Mounts=[]`; `/var/run/docker.sock` absent. | PASS |
| Memory and swap | `mem_limit: 512m`; **added** `memswap_limit: 512m` | Before: `MemorySwap=1073741824`, `memory.swap.max=536870912` (extra 512 MiB). After: `Memory=MemorySwap=536870912`, `memory.max=536870912`, `memory.swap.max=0`. | PASS after hardening |
| CPU | `cpus: 1.0` | `NanoCpus=1000000000`; cgroup `cpu.max=100000 100000`. | PASS |
| Processes | `pids_limit: 64` | cgroup `pids.max=64`; a bounded attempt to fork at most 70 sleeping children stopped at 62 with EAGAIN (11), then killed and reaped all children. `RLIMIT_NPROC` was unlimited; the cgroup limit is the enforced boundary. | PASS |
| Files and sockets | **added** soft/hard `nofile: 1024` | Before: `RLIMIT_NOFILE=(1024,524288)`; after: `(1024,1024)`. At most 1,100 `/dev/null` opens stopped after 1,021 new descriptors with EMFILE (24), then all were closed. Sockets share the FD ceiling; no independent socket-count ceiling was configured or claimed. | PASS for FD ceiling |
| Namespaces | Compose default private network, PID and mount namespaces | Container network/PID/mount namespace IDs differed from the workspace process. `NetworkMode` used the Compose bridge; `IpcMode=private`. User namespace ID matched the host and `uid_map` was identity mapped. | PASS for network/PID/mount; userns not enabled |
| Seccomp and LSM | Docker default seccomp; no explicit AppArmor requirement | `Seccomp: 2`, `Seccomp_filters: 1`. Daemon advertises `apparmor,profile=default` and inspect says `docker-default`, but `/proc/self/attr/current` said `unconfined`: **do not count AppArmor as enforced** on this nested host. SELinux not observed. | PASS seccomp; AppArmor NOT VERIFIED as protection |
| Termination and restart | `restart: unless-stopped` | Synthetic PID 1 with handler logged `term-received` and exited 0 after SIGTERM; separate SIGKILL exited 137. A normal synthetic process exit 42 under isolated `compose up` produced `RestartCount=6` and `Status=running`. `compose run` itself has `Restart=no`, so restart evidence comes from `compose up`. | PASS for signals and declared restart policy |
| OOM reporting | Docker cgroup memory budget | Separate `--memory=64m --memory-swap=64m` disposable probe attempted a bounded 96 MiB allocation: exit 137, `OOMKilled=true`. The 512 MiB worker budget was inspected, not exhausted. `OomKillDisable` was unset. | PASS for daemon reporting; worker-specific OOM untested |

No root filesystem, Docker socket or host mount was made writable to perform these tests. This host did not show enforced AppArmor or user-namespace remapping. The Docker bridge is **not** an egress policy; Gate 1B.2-D owns that control. The Compose service has a database URL in its environment; secret delivery and role separation remain Gates D and C respectively.

## RT test matrix

| Test | Evidence | Result |
| --- | --- | --- |
| RT-01 non-root | UID/GID 10001, setuid(0) EPERM | PASS |
| RT-02 filesystem | `/app` EROFS; `/tmp` write/read/delete; tmpfs mount options | PASS |
| RT-03 privilege | all effective/bounding capabilities zero; NoNewPrivs 1; not privileged | PASS |
| RT-04 host isolation | empty bind/volume mounts, absent Docker socket | PASS |
| RT-05 memory | cgroup 512 MiB and zero additional swap after fix | PASS |
| RT-06 process | `pids.max=64`, bounded fork denied at 62 children | PASS |
| RT-07 descriptors | soft/hard 1024; bounded open denied with EMFILE | PASS |
| RT-08 termination | SIGTERM handler exit 0, SIGKILL exit 137, no OOM flag | PASS |
| RT-09 recovery compatibility | PostgreSQL 16.16 `test_crawler_worker_recovery_r3.py`, fault framework and event trace: 18 passed; the recovery tests kill a child worker process after claim/processing, expire its lease and verify reclaim, ledger and stale-token rejection. This is not an end-to-end Docker-container kill against PostgreSQL. | PASS for existing repository mechanism; container/DB combined path NOT VERIFIED |
| RT-10 effective evidence | `docker inspect`, process `/proc`, cgroups, bounded behavior, isolated Compose restart | PASS |

## Reproduction commands and cleanup

The commands below show the actual command forms; the disposable database password is intentionally represented by `$B21_SYNTHETIC_PASSWORD` rather than recorded. It was a test-only value accepted by a disposable PostgreSQL trust-auth instance. Every running probe was bounded by `timeout` or by a short-lived container command, and every named test container was removed after inspection.

```sh
docker version
docker compose version
CRAWLER_DATABASE_URL="$B21_TEST_URL" docker compose --profile workers config --quiet
docker build -t levi-b21-worker:disposable backend
CRAWLER_DATABASE_URL="$B21_TEST_URL" docker compose --profile workers run -d --no-deps --name levi-b21-hardened --entrypoint python crawler-worker -c 'import time; time.sleep(180)'
docker inspect --format 'User={{.Config.User}} Readonly={{.HostConfig.ReadonlyRootfs}} Privileged={{.HostConfig.Privileged}} Memory={{.HostConfig.Memory}} Swap={{.HostConfig.MemorySwap}} CPUs={{.HostConfig.NanoCpus}} PIDs={{.HostConfig.PidsLimit}} Ulimits={{json .HostConfig.Ulimits}} Mounts={{json .Mounts}}' levi-b21-hardened
timeout 15s docker exec levi-b21-hardened python -c 'import os,resource; print(os.getuid(),os.getgid(),resource.getrlimit(resource.RLIMIT_NOFILE)); print({n:open("/sys/fs/cgroup/"+n).read().strip() for n in ("memory.max","memory.swap.max","cpu.max","pids.max")})'
timeout 15s docker exec levi-b21-hardened python -c 'import os; p="/app/.b21-probe"; t="/tmp/.b21-probe"; exec("try:\n open(p,\"w\").write(\"x\")\n print(\"root_write=UNEXPECTED_SUCCESS\")\nexcept OSError as e:\n print(\"root_write_errno\",e.errno)\n"); open(t,"w").write("ok"); print("tmp_write",open(t).read()); os.unlink(t); print("socket",os.path.exists("/var/run/docker.sock")); exec("try:\n os.setuid(0)\n print(\"setuid_root=UNEXPECTED_SUCCESS\")\nexcept OSError as e:\n print(\"setuid_root_errno\",e.errno)\n")'
timeout 15s docker exec levi-b21-hardened python -c 'import os; f=[]; exec("try:\n for i in range(1100):\n  f.append(os.open(\"/dev/null\",os.O_RDONLY))\n print(\"fd_ceiling=UNEXPECTED_SUCCESS\",len(f))\nexcept OSError as e:\n print(\"fd_count_before_emfile\",len(f),\"errno\",e.errno)\nfinally:\n for x in f: os.close(x)\n")'
timeout 15s docker exec levi-b21-hardened python -c 'import os,signal,time; children=[]; error=None; exec("try:\n for i in range(70):\n  try:\n   pid=os.fork()\n  except OSError as e:\n   error=e.errno\n   break\n  if pid==0:\n   time.sleep(5)\n   os._exit(0)\n  children.append(pid)\nfinally:\n for pid in children:\n  try: os.kill(pid,signal.SIGKILL)\n  except ProcessLookupError: pass\n for pid in children:\n  os.waitpid(pid,0)\n"); print("forked",len(children),"failure_errno",error)'
CRAWLER_DATABASE_URL="$B21_TEST_URL" docker compose --profile workers run -d --no-deps --name levi-b21-term --entrypoint python crawler-worker -c 'import signal,sys,time; signal.signal(signal.SIGTERM,lambda *_:(print("term-received",flush=True),sys.exit(0))); print("ready",flush=True); time.sleep(120)'
CRAWLER_DATABASE_URL="$B21_TEST_URL" docker compose --profile workers run -d --no-deps --name levi-b21-kill --entrypoint python crawler-worker -c 'import time; print("ready",flush=True); time.sleep(120)'
timeout 10s docker stop --timeout 3 levi-b21-term
timeout 10s docker kill --signal=KILL levi-b21-kill
docker inspect --format '{{.State.ExitCode}} {{.State.OOMKilled}}' levi-b21-term levi-b21-kill
timeout 15s docker run --name levi-b21-oom --network none --memory 64m --memory-swap 64m --pids-limit 8 --read-only python:3.12-slim python -c 'x=bytearray(96*1024*1024); print(len(x))'
docker inspect --format '{{.State.OOMKilled}} {{.State.ExitCode}}' levi-b21-oom
```

For the PostgreSQL regression, an isolated `postgres:16` container used tmpfs data, loopback exposure and no real data. Its test user/database matched `backend/scripts/postgres_ci.py`, which rejects any other identity. The first local `pytest` attempt from the workspace was stopped by that preflight before collection; migration from the workspace also hit the sandbox's socket restriction. The successful run used a disposable Docker network alias `postgres` and a read-only Python 3.12 test container:

```sh
docker run -d --name levi-b21-pg --tmpfs /var/lib/postgresql/data:rw,noexec,nosuid,nodev,size=512m -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=levi_b21_test -e POSTGRES_USER=levi_b21 -p 127.0.0.1:55433:5432 postgres:16
timeout 10s docker exec levi-b21-pg pg_isready -U levi_b21 -d levi_b21_test
docker network create levi-b21-pgnet
docker network connect --alias postgres levi-b21-pgnet levi-b21-pg
docker exec levi-b21-pg psql -U levi_b21 -d postgres -c 'CREATE ROLE levi_gate_1b1_ci LOGIN;'
docker exec levi-b21-pg psql -U levi_b21 -d postgres -c 'CREATE DATABASE levi_gate_1b1_ci OWNER levi_gate_1b1_ci;'
timeout 90s docker run --rm --network levi-b21-pgnet --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m -e PYTHONDONTWRITEBYTECODE=1 -e TEST_DATABASE_URL="postgresql+asyncpg://levi_gate_1b1_ci:$B21_SYNTHETIC_PASSWORD@postgres:5432/levi_gate_1b1_ci" -v /workspaces/Levi-Intelligence/backend:/workspace/backend:ro -w /workspace/backend levi-gate-r21-py312-tests:latest python scripts/postgres_ci.py migrate-and-verify
timeout 120s docker run --rm --network levi-b21-pgnet --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m -e PYTHONDONTWRITEBYTECODE=1 -e TEST_DATABASE_URL="postgresql+asyncpg://levi_gate_1b1_ci:$B21_SYNTHETIC_PASSWORD@postgres:5432/levi_gate_1b1_ci" -v /workspaces/Levi-Intelligence/backend:/workspace/backend:ro -w /workspace/backend levi-gate-r21-py312-tests:latest pytest -q -p no:cacheprovider tests/integration/test_crawler_worker_recovery_r3.py tests/integration/test_crawler_fault_framework.py tests/integration/test_crawler_event_trace_r3.py
docker rm --force levi-b21-pg
docker network rm levi-b21-pgnet
```

The isolated restart probe used a temporary `/tmp/levi-b21.override.yml` containing `services.crawler-worker.command: ["python", "-c", "import sys,time; print('ready', flush=True); time.sleep(2); sys.exit(42)"]`. Its commands were:

```sh
CRAWLER_DATABASE_URL="$B21_TEST_URL" docker compose -p levi-b21 -f docker-compose.yml -f /tmp/levi-b21.override.yml --profile workers up -d --no-deps crawler-worker
docker inspect --format 'Restart={{.HostConfig.RestartPolicy.Name}} Count={{.RestartCount}} Status={{.State.Status}}' levi-b21-crawler-worker-1
CRAWLER_DATABASE_URL="$B21_TEST_URL" docker compose -p levi-b21 -f docker-compose.yml -f /tmp/levi-b21.override.yml --profile workers down --volumes --remove-orphans
docker rm --force levi-b21-probe levi-b21-hardened levi-b21-term levi-b21-kill levi-b21-oom
```

The empty default-project network and volume created by `compose run` were verified unused and removed. `docker ps` showed no remaining test container.

## Validation, residual risk and rollback

`pytest -q tests/test_crawler_execution_boundary.py`: **10 passed**. Focused PostgreSQL 16.16 recovery/fault/event tests: **18 passed**. `ruff check app tests`: passed. `mypy app/workers/crawler.py --follow-imports=silent`: passed (no Python file changed). Compose configuration validation and `git diff --check`: passed. The R2.1 capacity matrix was not run. CI result for the final commit must be checked separately; it is not asserted by this local report.

Residual risks: AppArmor is reported by Docker but not observed inside the process; user namespaces are not remapped; there is no separate socket-count ceiling; the 512 MiB worker OOM path and live container-to-PostgreSQL recovery were not exercised; `unless-stopped` restarts a crash but cannot guarantee a clean application shutdown. B.2.2 must bound active task duration and shutdown before production claims. The database connection pool and queue-recovery saturation issues from the roadmap remain outside this checkpoint.

Rollback: revert only the `memswap_limit: 512m` and `ulimits.nofile` additions if the target Docker runtime rejects them or a measured workload cannot operate within the FD ceiling. Revalidate `docker compose config`, recreate only crawler-worker containers through a controlled deployment, and verify effective cgroups/rlimits before traffic. No job or ledger data needs deletion. Entry to B.2.2 requires an identified deployment target with the same effective controls, a synthetic job-deadline/shutdown test plan, and preserved PostgreSQL 16 recovery evidence.
