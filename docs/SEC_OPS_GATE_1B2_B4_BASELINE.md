# Gate 1B.2-B.4 baseline: saturated crawler recovery

The B.3 final commit `3a086e8d9e41d406dbdd9231b3570d83f715ec8f` is published on `feature/build-006a-secure-crawler`. GitHub Actions run `44944181581` completed successfully. The untracked `frontend/tsconfig.tsbuildinfo` is user data and remains untouched.

## Reproduction and transaction audit

`claim_next()` holds advisory transaction lock `681204117` and changes up to 32 expired `RUNNING` rows to `QUEUED` without checking either queue limit. The new regression test claims a job, fills its tenant queue to 100, expires its lease, invokes recovery through another tenant's claim, and requires the queue to remain at 100. Against B.3 it produces 101 and fails. Initial admission takes the same advisory lock and checks both queued counts; claim takes the lock and checks valid active leases (8 global, 2 per tenant). Cancellation and completion remove rows from capacity; neither adds queued rows. Expired retry is the other queued-increasing path.

The attempt ledger closes on expiration and generation increments only at a later claim. Token and expiry checks fence stale writes. Cancellation can terminate a `RUNNING` row. Five attempts exhaust before requeue. The existing status enum has no pending status. An expired `RUNNING` row with its token cleared and expired timestamp retained can durably represent recovery pending without schema or public status changes. Valid active counts require an unexpired lease, so this row consumes no active capacity; queued counts require `QUEUED`, so it consumes no queued capacity. Polling continues to expose `RUNNING` until admitted for retry or terminal, which is a known presentation limitation.

Selection must be bounded and deterministic. Repeatedly scanning only the oldest 32 expired rows can strand other tenants behind a saturated tenant. A deferred `next_attempt_at` and ordered bounded scan will rotate blocked rows; admission and recovery must serialize capacity decisions using the existing lock. Continuous admission also needs recovery priority when queue space appears.
