from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.prospecting import CompanyStatus, DecisionMakerRole, DecisionMakerStatus, ProspectingRunStatus


class ICPProfileCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    industry: str = Field(min_length=2, max_length=120)
    geography: dict[str, Any] = Field(default_factory=dict)
    company_size: dict[str, Any] = Field(default_factory=dict)
    criteria: dict[str, Any] = Field(default_factory=dict)
    exclusions: list[str] = Field(default_factory=list, max_length=100)
    is_active: bool = True


class ICPProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    description: str | None
    industry: str
    geography: dict[str, Any]
    company_size: dict[str, Any]
    criteria: dict[str, Any]
    exclusions: list[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ProspectingRunCreate(BaseModel):
    icp_profile_id: UUID
    query: str = Field(min_length=3, max_length=500)
    provider: str = Field(default="manual", pattern=r"^(manual|apify)$")
    filters: dict[str, Any] = Field(default_factory=dict)


class ProspectingRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    icp_profile_id: UUID
    provider: str
    query: str
    filters: dict[str, Any]
    status: ProspectingRunStatus
    companies_found: int
    companies_imported: int
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    attempts: int = 0
    max_attempts: int = 3
    last_error: str | None = None
    cancel_requested_at: datetime | None = None


class CompanyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    website_url: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=40)
    email: EmailStr | None = None
    address: dict[str, Any] = Field(default_factory=dict)
    industry: str | None = Field(default=None, max_length=120)
    employee_count: int | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=5000)
    source_name: str = Field(default="manual", max_length=160)
    source_url: str | None = Field(default=None, max_length=1000)

    @field_validator("website_url", "source_url")
    @classmethod
    def validate_urls(cls, value: str | None) -> str | None:
        if value is not None:
            from urllib.parse import urlsplit

            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("URL must be a valid http or https URL")
        return value


class CompanyStatusUpdate(BaseModel):
    status: CompanyStatus


class CompanyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    normalized_domain: str | None
    website_url: str | None
    phone: str | None
    email: str | None
    address: dict[str, Any]
    industry: str | None
    employee_count: int | None
    description: str | None
    status: CompanyStatus
    qualification_score: int | None
    qualification_reasons: list[str]
    first_source: str | None
    source_url: str | None
    created_at: datetime
    updated_at: datetime
    website_status: str = "unknown"
    website_checked_at: datetime | None = None
    enrichment_status: str = "pending"
    qualification_confidence: float | None = None
    qualification_breakdown: dict[str, Any] = Field(default_factory=dict)
    opportunity_score: int | None = None


class SourceRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    company_id: UUID
    source_type: str
    source_name: str
    source_url: str | None
    external_id: str | None
    captured_data: dict[str, Any]
    captured_at: datetime


class DecisionMakerCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=160)
    job_title: str | None = Field(default=None, max_length=160)
    role_category: DecisionMakerRole = DecisionMakerRole.OTHER
    professional_email: EmailStr | None = None
    professional_phone: str | None = Field(default=None, max_length=40)
    profile_url: str | None = Field(default=None, max_length=1000)
    source_url: str | None = Field(default=None, max_length=1000)
    evidence: dict[str, Any] = Field(default_factory=dict)


class DecisionMakerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    company_id: UUID
    full_name: str
    job_title: str | None
    professional_email: str | None
    professional_phone: str | None
    profile_url: str | None
    source_url: str | None
    evidence: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    role_category: DecisionMakerRole = DecisionMakerRole.OTHER
    status: DecisionMakerStatus = DecisionMakerStatus.UNCONFIRMED
    confidence_score: int = 0
    confidence_level: str = "insufficient"
    first_seen_at: datetime | None = None
    last_verified_at: datetime | None = None
