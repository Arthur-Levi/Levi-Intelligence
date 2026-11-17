"""Opt-in integration test. Requires a dedicated, migrated PostgreSQL test database."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.identity import AuditEvent, Tenant

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.mark.asyncio
async def test_postgres_persists_and_isolates_tenant_audit_rows() -> None:
    assert DATABASE_URL is not None
    assert "test" in DATABASE_URL.rsplit("/", 1)[-1].lower(), (
        "Refusing integration test: database name must contain 'test'"
    )
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid4(), uuid4()
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        async with factory() as session:
            transaction = await session.begin()
            try:
                session.add_all([
                    Tenant(id=tenant_a, name="Tenant Test A", slug=f"tenant-a-{tenant_a.hex[:8]}"),
                    Tenant(id=tenant_b, name="Tenant Test B", slug=f"tenant-b-{tenant_b.hex[:8]}"),
                ])
                await session.flush()
                session.add_all([
                    AuditEvent(tenant_id=tenant_a, action="test.a", resource_type="test"),
                    AuditEvent(tenant_id=tenant_b, action="test.b", resource_type="test"),
                ])
                await session.flush()
                result = await session.scalars(
                    select(AuditEvent).where(AuditEvent.tenant_id == tenant_a)
                )
                rows = result.all()
                assert len(rows) == 1
                assert rows[0].action == "test.a"
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
