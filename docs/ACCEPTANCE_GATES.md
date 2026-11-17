# Definition of done and release gates

A module is not complete until all applicable items are met:

- Functional behavior matches documented acceptance criteria.
- Unit, integration and end-to-end tests pass in a disposable environment.
- Tenant isolation and authorization tests pass.
- Input validation, failure handling, idempotency and retry behavior are tested.
- Logs, metrics and cost attribution exist for background/AI work.
- Data provenance and uncertainty are visible to users.
- Privacy, retention and source-policy implications are documented.
- API and operator documentation are updated.
- No secrets, real prospect personal data or production dumps are committed.

## Gate 0 — Foundation
- [ ] Backend starts and health endpoints respond.
- [ ] Frontend builds and renders the product shell.
- [ ] PostgreSQL and Redis start in local compose.
- [ ] CI runs lint, type checks and tests.
- [ ] Configuration and secret handling reviewed.

## Gate 1 — Lead discovery
- [ ] User can define a sector/geography ICP and authorize a discovery run.
- [ ] Apify adapter stores raw source reference and normalized organization.
- [ ] Duplicate organizations are merged deterministically with audit history.
- [ ] Run is idempotent and resumable; provider errors are visible.
