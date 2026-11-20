"""Diagnostic event schema rejects raw payloads and survives logger failures."""

import json
from uuid import uuid4

import pytest

from app.crawler_jobs import events


def test_structured_event_allowlist_and_redaction(monkeypatch):
    captured = []
    monkeypatch.setattr(events.logger, "info", captured.append)
    tenant, job, attempt, worker = uuid4(), uuid4(), uuid4(), uuid4()
    events.emit(
        "claim",
        tenant_id=tenant,
        job_id=job,
        attempt_id=attempt,
        worker_id=worker,
        outcome="running",
    )
    payload = json.loads(captured[0])
    assert payload == {
        "event_type": "claim",
        "timestamp": payload["timestamp"],
        "tenant_id": str(tenant),
        "job_id": str(job),
        "attempt_id": str(attempt),
        "worker_id": str(worker),
        "outcome": "running",
        "correlation_id": str(job),
    }
    assert "lease_token" not in captured[0]
    with pytest.raises(ValueError):
        events.emit("claim", tenant_id=tenant, outcome="secret")


def test_logging_failure_is_best_effort(monkeypatch):
    def broken(_message):
        raise OSError("diagnostic sink unavailable")

    monkeypatch.setattr(events.logger, "info", broken)
    events.emit("completion", tenant_id=uuid4(), outcome="completed")
