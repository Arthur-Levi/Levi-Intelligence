import json
from datetime import datetime
from hashlib import sha256
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.models.prospecting import CrawlerJob


class CrawlerJobInput(BaseModel):
    """Bounded handoff payload; contains no credentials or arbitrary options."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    tenant_id: UUID
    company_id: UUID
    website_url: str = Field(min_length=8, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=128)
    lease_token: UUID | None = None


class CrawlerJobResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["verified", "reachable_non_html", "http_error"]
    status_code: int = Field(ge=100, le=599)
    checked_at: datetime
    emails: list[str] = Field(default_factory=list, max_length=10)
    phones: list[str] = Field(default_factory=list, max_length=10)


class CrawlerJobView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    status: str
    result: CrawlerJobResult | None = None
    failure_code: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


def validate_job_input(payload: CrawlerJobInput) -> CrawlerJobInput:
    """Apply URL syntax validation before a payload crosses the process boundary."""
    parsed = HttpUrl(payload.website_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("crawler job URL must use http or https")
    return payload


def build_crawler_job(
    *, tenant_id: UUID, company_id: UUID, actor_user_id: UUID,
    website_url: str, idempotency_key: str,
) -> CrawlerJob:
    """Build a minimal durable job and a stable fingerprint for idempotent submission."""
    fingerprint = sha256(json.dumps(
        {"company_id": str(company_id), "website_url": website_url}, sort_keys=True
    ).encode()).hexdigest()
    return CrawlerJob(
        tenant_id=tenant_id,
        company_id=company_id,
        created_by_user_id=actor_user_id,
        website_url=website_url,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
    )
