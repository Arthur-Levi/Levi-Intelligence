"""Independent synthetic API process for the R3 admission race."""

import asyncio
import json
import os
import sys
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.dependencies import TenantContext
from app.api.v1.endpoints.prospecting import create_company_enrichment_job
from app.models.identity import Membership, Tenant, User


async def main() -> None:
    tenant_id, user_id, company_id, key = sys.argv[1:5]
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"], pool_size=1, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            user = await session.get(User, UUID(user_id))
            tenant = await session.get(Tenant, UUID(tenant_id))
            membership = await session.scalar(select(Membership).where(
                Membership.user_id == UUID(user_id),
                Membership.tenant_id == UUID(tenant_id)))
            assert user is not None and tenant is not None and membership is not None
            context = TenantContext(
                user=user, tenant=tenant, membership=membership,
            )
        async with factory() as session:
            try:
                response = await create_company_enrichment_job(
                    UUID(company_id),
                    Request({"type": "http", "client": ("127.0.0.1", 1), "headers": []}),
                    key, context, session,
                )
                print(json.dumps({"status": "accepted", "job_id": str(response.id)}), flush=True)
            except HTTPException as exc:
                print(json.dumps({"status": "rejected", "code": exc.status_code,
                                  "detail": exc.detail}), flush=True)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
