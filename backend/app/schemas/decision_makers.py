from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.prospecting import DecisionMakerRole, DecisionMakerStatus


class DecisionMakerEvidenceCreate(BaseModel):
    claim_type: str = Field(pattern=r"^(role|company_affiliation|ownership)$")
    source_type: str = Field(
        pattern=r"^(official_registry|company_website|licensed_provider|professional_publication|public_directory|manual_attestation)$"
    )
    source_name: str = Field(min_length=2, max_length=160)
    source_url: str = Field(min_length=8, max_length=1000)
    source_published_at: datetime | None = None
    excerpt: str | None = Field(default=None, max_length=500)

    @field_validator("source_url")
    @classmethod
    def validate_source(cls, value: str) -> str:
        from app.services.decision_maker_intelligence import validate_public_source_url

        return validate_public_source_url(value)


class DecisionMakerEvidenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    claim_type: str
    source_type: str
    source_name: str
    source_url: str
    source_published_at: datetime | None
    observed_at: datetime
    excerpt: str | None
    authority_weight: int
    is_current: bool


class DecisionMakerIntelligenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    company_id: UUID
    full_name: str
    job_title: str | None
    role_category: DecisionMakerRole
    professional_email: str | None
    professional_phone: str | None
    profile_url: str | None
    status: DecisionMakerStatus
    confidence_score: int
    confidence_level: str
    first_seen_at: datetime
    last_verified_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DecisionMakerChangeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    change_type: str
    before: dict
    after: dict
    created_at: datetime


class DecisionMakerUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=160)
    job_title: str | None = Field(default=None, max_length=160)
    role_category: str | None = Field(
        default=None, pattern=r"^(owner|partner|director|manager|procurement|marketing|other)$"
    )
    professional_email: str | None = Field(default=None, max_length=320)
    professional_phone: str | None = Field(default=None, max_length=40)
    status: str | None = Field(default=None, pattern=r"^(unconfirmed|former|disputed)$")
