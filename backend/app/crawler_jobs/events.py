"""Best-effort, bounded diagnostic lifecycle events with an allowlisted schema."""

import json
import logging
from datetime import UTC, datetime
from uuid import UUID

logger = logging.getLogger("levi.crawler_lifecycle")

EVENTS = frozenset(
    {
        "admission",
        "claim",
        "lease_renewal",
        "lease_expiration",
        "recovery",
        "retry_scheduled",
        "cancellation",
        "completion",
        "failure",
        "capacity_rejection",
        "idempotency_conflict",
    }
)
OUTCOMES = frozenset(
    {
        "accepted",
        "rejected",
        "running",
        "renewed",
        "expired",
        "queued",
        "cancelled",
        "completed",
        "failed",
        "retry_exhausted",
        "conflict",
    }
)


def emit(
    event_type: str,
    *,
    tenant_id: UUID,
    job_id: UUID | None = None,
    attempt_id: UUID | None = None,
    worker_id: UUID | None = None,
    outcome: str,
    correlation_id: UUID | None = None,
) -> None:
    """Never accept payloads, tokens, URLs, exception text or free-form details."""
    if event_type not in EVENTS or outcome not in OUTCOMES:
        raise ValueError("invalid lifecycle event")
    event = {
        "event_type": event_type,
        "timestamp": datetime.now(UTC).isoformat(),
        "tenant_id": str(tenant_id),
        "job_id": str(job_id) if job_id else None,
        "attempt_id": str(attempt_id) if attempt_id else None,
        "worker_id": str(worker_id) if worker_id else None,
        "outcome": outcome,
        "correlation_id": str(correlation_id or job_id or tenant_id),
    }
    try:
        logger.info(json.dumps(event, separators=(",", ":")))
    except Exception:
        # Diagnostic logging cannot veto a committed job transaction.
        pass
