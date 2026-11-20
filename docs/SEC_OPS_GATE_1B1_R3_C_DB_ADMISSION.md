# Gate 1B.1-R3-C: PostgreSQL failures and concurrent admission

## Environment and method

Branch `feature/build-006a-secure-crawler`, base R3-B commit
`395024116041a653aff9af4e8e68955106e19f4b`. All tests ran against the
identity-verified disposable PostgreSQL 16 service with Alembic head 0007,
512 MiB database tmpfs, a 512 MiB Python-container limit and two CPUs. No
production endpoint or external website was contacted. The test suite has a
120-second outer timeout, 2–15 second operation timeouts, at most four local
contenders or two child API processes, and explicit child cleanup. The
unavailability test restores database access in a nested `finally` block;
fixtures remove synthetic rows in `finally`.

## Database interruption evidence

`test_crawler_db_failures_r3.py` passes against real PostgreSQL transactions.
The connection interruption hook calls `asyncpg.Connection.terminate()` on
the actual connection immediately before the selected SQL is sent. The
repository raises a database exception; the test reads state through a new
session before allowing any retry.

| Scenario | Test and verified durable result |
|---|---|
| Connection lost before claim SQL | Advisory-lock query interrupted; queued job remains at attempt 0; later claim succeeds |
| Connection lost during claim | Attempt insert interrupted; claim transaction rolls back; no orphan attempt; later claim succeeds |
| Connection lost during result persistence | Source insert interrupted; result transaction rolls back; job stays running with no source; valid retry completes |
| Connection lost during retry scheduling | Job update interrupted; expired running job and open attempt remain until later recovery |
| Database temporarily unavailable | `ALLOW_CONNECTIONS false` on the verified disposable database rejects a fresh checkout; `finally` restores `true`; a subsequent claim succeeds |
| Ambiguous commit acknowledgment | Test-only `after_commit` error is delivered to the client; authoritative fresh read finds completed job, completed attempt and exactly one source; no blind repeat |
| Pool exhaustion | One-connection pool is held; 200 ms checkout times out explicitly; release permits a later claim |

The post-commit error is deterministic **client ambiguity**, not a proven
packet-loss event. The database row read, rather than the exception, decides
whether a result committed. No false-success response was observed. SQLAlchemy
rollback and attempt/source assertions show no partial commit in the tested
interruptions. Database availability is changed only after the test identity
preflight succeeds; the maintenance connection is disposed after restoration.

## Admission evidence

`test_crawler_admission_r3.py` calls the real
`create_company_enrichment_job()` implementation for every boundary decision.
Bulk direct inserts only prepare the synthetic 9,999-row starting fixture, in
500-row batches. They do not stand in for admission decisions. The fixture
uses 99 extra tenant/company pairs with at most 100 queued jobs per tenant,
then removes every extra row in `finally`.

| Transition or race | Verified result |
|---|---|
| Tenant 99 → 100, four distinct-key contenders | One accepted, three 429 `crawler_tenant_queue_full`, exactly 100 queued |
| Four same-key contenders at tenant 99 | One logical job ID returned four times, exactly 100 queued |
| Global 9,999 → 10,000, two tenants racing | One accepted, one 429 `crawler_queue_full`, exactly 10,000 queued |
| Duplicate key after global saturation | Existing job returned; queue remains exactly 10,000 |
| Admission racing claim | Two accepted admissions, two claims; tenant queued count ≤100 and global running count 2 |
| Two independent API subprocesses racing tenant slot | One accepted, one 429, exactly 100 queued |

The earlier PostgreSQL lifecycle tests additionally cover repeated keys across
tenants, different-fingerprint conflict, global eight-active and tenant
two-active claim caps, tenant-scoped GET/claim/result, and cancellation. The
admission queue cap counts `QUEUED`; active capacity counts unexpired `RUNNING`
leases. These are separate controls. The global advisory lock remains in place
for atomic admission and claim count decisions.

Application and repository predicates reject cross-tenant operations, and the
attempt ledger has a composite tenant/job foreign key. PostgreSQL row-level
security is absent. A database principal with unrestricted SQL privileges can
read or modify other tenants' rows and bypass application limits; this gate
does not claim database-enforced tenant isolation.

## Executed checks and decision

- `timeout 120 pytest -q tests/integration/test_crawler_db_failures_r3.py tests/integration/test_crawler_admission_r3.py`:
  **12 passed in 5.40 s**.
- `ruff check app tests`: passed.
- `mypy tests/integration/crawler_admission_child.py tests/integration/test_crawler_admission_r3.py tests/integration/test_crawler_db_failures_r3.py --follow-imports=silent`:
  passed, no issues in three files.

R3-C is **PASS for the required synthetic PostgreSQL interruption and admission
scenarios**, with the explicit post-commit wire-loss and privileged-SQL
limitations above. This decision does not assert general exactly-once external
effects. The final gate decision requires R3-D regression and CI evidence.
