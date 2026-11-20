# SEC/OPS Gate 1B.2-B.1 — Legacy enrichment route migration

## Scope and decision

This checkpoint migrates only `POST /api/v1/companies/{company_id}/enrich` to the durable crawler admission contract. Gate decision: **PASS within the repository-tested B.1 scope**. This does not establish production worker isolation; B.2–B.5 remain separate gates.

## Contract before and after

| | Before B.1 (`e5ef827^`) | After B.1 (`e5ef827` onward) |
| --- | --- | --- |
| Path and method | `POST /api/v1/companies/{company_id}/enrich` | Same URL and method |
| Authorization | Bearer token, tenant membership, `companies:write`; company looked up in tenant | Same |
| Request | No body; no idempotency header | No body; required `Idempotency-Key` header of 8–128 characters |
| Success | `200 OK`, `CompanyResponse`, after `verify_and_extract()` in API and direct `SourceRecord` write | `202 Accepted`, `CrawlerJobResponse`, after durable PostgreSQL admission; no API website fetch or immediate verification source |
| Errors | 401/403 authorization, 404 absent or other-tenant company, 422 absent/unsafe website, 502 fetch failure | 401/403 authorization, 404 absent or other-tenant company, 422 absent website or invalid/missing header, 409 reused key with different company/URL fingerprint, 429 tenant/global queued capacity |
| Follow-up | Company response contained completed website fields | Poll `GET /api/v1/crawler-jobs/{job_id}` with `companies:read`; this lookup is tenant-scoped |

Preserving the endpoint URL **does not preserve the old synchronous response contract**. The canonical `POST /api/v1/companies/{company_id}/enrich/jobs` remains supported with the same admission contract. Both URLs call `create_company_enrichment_job` in `backend/app/api/v1/endpoints/prospecting.py`. The handler uses `build_crawler_job` from `backend/app/crawler_jobs/contracts.py`, the same tenant lookup, fingerprint, transaction advisory lock (`681204117`), duplicate-key handling, 10,000 global and 100 tenant queued limits, `company.enrichment_queued` audit event, commit, and response serialization. Active limits, retries, lease fencing and result persistence remain in the unchanged worker path.

Example using the actual `CrawlerJobResponse` fields (synthetic identifiers and timestamp):

```http
POST /api/v1/companies/11111111-1111-4111-8111-111111111111/enrich HTTP/1.1
Authorization: Bearer <token>
Idempotency-Key: company-11111111-verification-1

HTTP/1.1 202 Accepted
Content-Type: application/json

{"id":"22222222-2222-4222-8222-222222222222","company_id":"11111111-1111-4111-8111-111111111111","status":"queued","result":null,"failure_code":null,"created_at":"2026-11-20T21:00:00Z","started_at":null,"finished_at":null}
```

The client must supply a stable key for retries of one logical request, store the returned job ID, poll the tenant-scoped status URL, and handle terminal job status and failure codes. It must not parse the `202` body as `CompanyResponse` or assume website data is available immediately. A different company or URL with the same tenant/key returns 409; a replay with the same fingerprint returns the same logical job, including when the queue is full. The key must not be generated afresh for each retry.

## Consumer compatibility

Repository search of `frontend/src` found no HTTP client or caller of either enrichment route; `frontend/src/App.tsx` is a static dashboard. The backend's former synchronous enrichment test was migrated to worker-result behavior in `backend/tests/intelligence/crawler/test_company_enrichment.py`. The OpenAPI regression test in `backend/tests/test_legacy_enrichment_contract_b1.py` asserts both POST operations expose required `Idempotency-Key`, bearer security, `202`, and `CrawlerJobResponse`. Historical documents such as `docs/BUILD_004.md` and the 1B.2-A baseline describe the pre-B.1 behavior as a historical snapshot; this document records the new contract. External clients and deployed consumers are not inventoried in this repository, so their migration must be coordinated before deployment.

## Security evidence and limitations

`backend/tests/integration/test_legacy_enrichment_b1.py` uses synthetic `.test` URLs and patches both `verify_and_extract` and `SecureHTTPTransport.fetch` to fail if invoked by API admission. It asserts route equivalence, idempotent replay, cross-tenant rejection, missing URL and header behavior, 409 fingerprint conflict, authorization, polling, no immediate `website_verification` source, one queue audit, and the tenant queued limit. `backend/tests/integration/test_crawler_admission_r3.py` checks the legacy route at the global queued limit. The B.1 route changes neither the worker repository nor schema, leases, retry, cancellation, or attempt ledger.

The prior CI run for `b472757` ([run 44822175017](https://github.com/Arthur-Levi/Levi-Intelligence/actions/runs/44822175017)) failed one integration test because its `pytest.raises` context manager was used in an `async with` statement. Commit `1b4a906` corrected only that test context. Local focused route/security tests: **29 passed** (`pytest -q tests/test_legacy_enrichment_contract_b1.py tests/test_crawler_execution_boundary.py tests/test_prospecting_tenant_isolation.py tests/test_tenant_scoping.py tests/test_security.py tests/intelligence/crawler/test_company_enrichment.py`). Local backend unit regression: **224 passed** (`pytest -q --ignore=tests/integration --ignore=tests/test_health.py`); the local Python 3.14 TestClient hangs on that file, so the Python 3.12 CI backend job is the full-suite evidence. Local `ruff check app tests`, `mypy app/api/v1/endpoints/prospecting.py tests/test_legacy_enrichment_contract_b1.py tests/intelligence/crawler/test_company_enrichment.py tests/integration/test_legacy_enrichment_b1.py tests/integration/test_crawler_admission_r3.py --follow-imports=silent` (no issues in five files), and `git diff --check` passed.

The CI run for `1b4a906` ([run 44822858044](https://github.com/Arthur-Levi/Levi-Intelligence/actions/runs/44822858044)) completed successfully: PostgreSQL **16** integration (`pytest -q tests/integration`) **50 passed**, Python 3.12 backend (`pytest -q --ignore=tests/integration`) **226 passed, 1 warning**, CI Ruff passed, and frontend `npm ci` plus `npm run build` passed. The PostgreSQL job verified disposable test identity and applied migrations before testing. CI does not run Mypy; the local affected-module check above supplies that evidence.

The shared API admission removes website crawling and direct verification-source creation from the API process. It does not establish runtime worker isolation, separate database roles, egress policy, or compatibility of unknown external clients. No exactly-once claim is made for external website effects.

## Final validation and decision

**PASS, limited to the B.1 route migration and repository-visible consumers.** Both routes admit through one durable PostgreSQL path; the old URL no longer invokes website crawling in the API; tenant, idempotency, capacity, polling and no-premature-result tests pass. The prior failing CI test was corrected and the subsequent full CI run passed. External consumer compatibility remains unverified because this repository contains no inventory of deployed clients; rollout owners must migrate callers expecting `200 CompanyResponse`. No B.2 work is included in this checkpoint.
