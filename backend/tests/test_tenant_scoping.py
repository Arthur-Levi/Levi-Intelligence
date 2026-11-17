from uuid import uuid4

from sqlalchemy import select

from app.models.identity import AuditEvent


def test_audit_query_is_explicitly_scoped_to_one_tenant() -> None:
    tenant_id = uuid4()
    statement = select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "audit_events.tenant_id" in compiled
    assert tenant_id.hex in compiled.replace("-", "")
