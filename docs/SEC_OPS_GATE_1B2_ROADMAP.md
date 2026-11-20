# SEC/OPS Gate 1B.2 — implementation roadmap

This plan follows the [1B.2-A architecture baseline](SEC_OPS_GATE_1B2_A_ARCHITECTURE_BASELINE.md)
and [threat register](SEC_OPS_GATE_1B2_A_THREAT_MODEL.md). Gate 1B.2-A is a
**PASS for documentation and design only**. Gate 1B.1-R3's synthetic
PostgreSQL guarantees remain regression requirements, not production approval.
Use disposable data and PostgreSQL 16 for all negative tests. No gate may claim
effective deployment security from Compose text alone. Keep B → C → D → E:
B establishes a bounded runtime and addresses the direct API crawl path; C
then limits that runtime's DB authority; D binds credentials, egress and audit
to the chosen runtime and role; E challenges the combined system. If a chosen
deployment target cannot supply the required controls, record **INCONCLUSIVE**
and select an implementable target before advancing.

## 1B.2-B — worker runtime isolation

- **Entry:** Choose a disposable deployment target and document how to inspect
  its effective UID, capabilities, mounts, namespaces, cgroups and network
  attachment. Freeze current API route semantics and R3 regression commands.
- **Implement:** Start with the compatibility route in
  `backend/app/api/v1/endpoints/prospecting.py`: decide and implement a
  client-compatible transition so `POST /companies/{id}/enrich` no longer
  executes `verify_and_extract()` inside the API process, or explicitly disable
  it with a tested contract. Keep the asynchronous admission path authoritative.
  Enforce worker non-root identity, read-only root, bounded tmpfs, no host/socket
  mounts, capability drop, no-new-privileges, process/CPU/memory/swap/FD/socket
  ceilings and an absolute job deadline with bounded shutdown. Explicitly set
  and measure DB pool overflow. Define a full-queue recovery policy: current
  `claim_next` can requeue an expired job without checking queued counts. Keep
  jobs recoverable and the ledger intact while preserving the shared advisory
  lock and lease semantics.
- **Tests:** API bypass/compatibility test; disposable container inspection;
  bounded long-running, SIGTERM/SIGKILL, cancellation and lease recovery tests;
  recovery with both global and tenant queues at capacity;
  R3 lifecycle/admission/ledger regression against isolated PostgreSQL 16.
- **PASS:** Every deployed crawler call crosses the worker boundary; effective
  runtime controls match declared bounds; long tasks terminate or lose lease
  safely; old token writes fail; queue 10,000/100, active 8/2, five attempts,
  idempotent admission and one committed result remain true. Record actual FD,
  process, memory, CPU and connection maxima under a bounded workload.
- **Depends on:** A inventory and a disposable runtime. **Non-goals:** DB RLS,
  production egress or secret rotation. **Rollback:** Retain the last accepted
  job/ledger schema and R3 repository path; stop new worker instances, let
  leases expire and recover, and revert the runtime/route deployment only after
  the client compatibility path is defined. Never delete queued jobs or ledger.

## 1B.2-C — tenant and database security

- **Entry:** B's effective worker identity and all SQL operations are known;
  capture a disposable copy of migration and role grants. Decide whether global
  worker claim is compatible with tenant-scoped RLS or needs a narrow broker/
  security-definer interface; document the decision before applying policy.
- **Implement:** Separate migration owner, API and crawler DB roles; grant only
  required tables/operations; prevent runtime DDL, role creation and bypass of
  tenant checks. If RLS is selected, define `USING` and `WITH CHECK` for every
  tenant table touched by API and worker, handle global claim safely, and test
  owner/BYPASSRLS behavior. If RLS is rejected, provide an equivalent narrow
  authenticated database or result API boundary and state its residual risk.
  Preserve shared advisory lock, transaction scope and attempt ledger FKs.
- **Tests:** Grant introspection and negative SQL for cross-tenant reads/writes,
  status edits, direct admission, source insertion and DDL; both tenant API
  users and a compromised worker role; R3-A/B/C/D and migration regression on
  PostgreSQL 16. Verify the worker can still recover expired jobs and update
  ledger/source/company within its authorized interface.
- **PASS:** The runtime roles cannot inspect/mutate another tenant outside an
  explicitly authorized global claim and its fenced job/result scope, or
  bypass intended object privileges; migration role is not
  used at runtime; RLS decision is executable and documented; all R3 counts,
  locks, fencing, cancellation and rollback tests pass with actual roles.
  Numeric queue caps remain enforced by serialized application logic, not an
  unsupported RLS claim.
- **Depends on:** B boundary and global-claim decision. **Non-goals:** Egress,
  general external-effect exactly-once, legacy ledger backfill. **Rollback:**
  Revoke new runtime credentials, restore the prior role/policy configuration
  only in a disposable or planned migration rollback, and keep data/schema and
  active leases intact; rehearse rollback with claims paused.

## 1B.2-D — secrets, egress and operational controls

- **Entry:** B runtime and C role endpoints are fixed; deployment owner can
  inspect effective network policy and credential delivery. Inventory every
  required DNS, PostgreSQL, public HTTP(S), logging and optional provider path.
- **Implement:** Deliver separate API/worker credentials through an approved
  secret mechanism, rotation and revocation procedure; no credential in image,
  logs or artifact. Enforce default-deny egress with explicit DNS, DB and public
  web allowances and denial of loopback, private/link-local, metadata, control
  plane and Docker endpoints for both IPv4/IPv6. Preserve URL/DNS/robots checks.
  Define audit-log sink ACL, retention, correlation, failure handling and
  authoritative DB-ledger reconciliation. Pin trusted build inputs, define
  SBOM/dependency/image scanning and retained evidence.
- **Tests:** Inspect effective network and secret policy; synthetic allowed and
  denied destination, redirect and DNS-rebinding probes inside disposable
  runtime; rotate/revoke test credential; redaction/rollback/log-loss tests;
  check CI permissions, image digest and artifact provenance.
- **PASS:** Denied destinations cannot be reached even by a process that
  bypasses Python URL checks; allowed DNS/DB/public web paths still function;
  secrets do not appear in logs, image layers or artifacts; rotation leaves
  no stale active credential; authoritative audit reconstruction succeeds when
  diagnostics fail; supply-chain checks produce retained evidence.
- **Depends on:** B runtime, C roles. **Non-goals:** Real website scanning,
  production penetration test, arbitrary HTTP exactly-once. **Rollback:**
  Revert a faulty policy/credential only through a controlled deployment with
  synthetic connectivity checks; retain old secret for a bounded overlap if
  required, then revoke it; do not expose private egress to restore service.

## 1B.2-E — adversarial acceptance

- **Entry:** B, C and D each have committed evidence, disposable runtime
  access and a frozen deployment configuration. No outstanding high-priority
  threat lacks a verified mitigation or accepted explicit residual risk.
- **Implement:** Acceptance harness and report only; repair defects in their
  owning gate, then rerun E. Keep the R3 schema, queue and ledger contracts.
- **Tests:** Full backend regression and PostgreSQL 16 integration; multi-
  process admission/claim races; worker kill/restart, timeout, cancellation and
  retry exhaustion; cross-tenant SQL/API attempts; unsafe egress and secret
  disclosure probes; log loss and external-effect replay; effective container,
  cgroup, network, role, grant and CI artifact inspections. Use synthetic
  destinations and data only; no full capacity matrix unless a measured limit
  changed and requires targeted remeasurement.
- **PASS:** Zero unauthorized tenant operations, private egress or credential
  leaks in the specified probes; effective limits and role grants match policy;
  no duplicate committed result, stale lease acceptance, sixth attempt or
  vanished job; 10,000/100 queued and 8/2 active ceilings hold; all required
  CI jobs pass on the acceptance SHA. Report `PASS`, `INCONCLUSIVE` or `FAIL`
  per control, with commands, artifacts, SHA and remaining limits. Production
  release remains a separate decision.
- **Depends on:** B–D. **Non-goals:** General exactly-once external execution,
  real-customer/real-website tests. **Rollback:** On failed acceptance, do not
  promote; revert the offending runtime/policy/role change in the disposable
  environment, preserve jobs/ledger, and rerun the owning gate plus E.

## First B implementation task

Write the compatibility-route contract and a failing test that shows
`POST /companies/{company_id}/enrich` currently invokes
`verify_and_extract()` in the API process. Choose the API response/migration
behavior with existing clients before changing it. This is B work; A makes no
runtime change.
