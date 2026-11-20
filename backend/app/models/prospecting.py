import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKey


class ProspectingRunStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CrawlerJobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DecisionMakerRole(enum.StrEnum):
    OWNER = "owner"
    PARTNER = "partner"
    DIRECTOR = "director"
    MANAGER = "manager"
    PROCUREMENT = "procurement"
    MARKETING = "marketing"
    OTHER = "other"


class DecisionMakerStatus(enum.StrEnum):
    CURRENT = "current"
    UNCONFIRMED = "unconfirmed"
    FORMER = "former"
    DISPUTED = "disputed"


class CompanyStatus(enum.StrEnum):
    NEW = "new"
    QUALIFIED = "qualified"
    CONTACTED = "contacted"
    CUSTOMER = "customer"
    DISQUALIFIED = "disqualified"


class ICPProfile(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "icp_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_icp_profiles_tenant_name"),
        UniqueConstraint("tenant_id", "id", name="uq_icp_profiles_tenant_id_id"),
        Index("ix_icp_profiles_tenant_active", "tenant_id", "is_active"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    industry: Mapped[str] = mapped_column(String(120), nullable=False)
    geography: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    company_size: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    criteria: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    exclusions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ProspectingRun(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "prospecting_runs"
    __table_args__ = (
        Index("ix_prospecting_runs_tenant_created", "tenant_id", "created_at"),
        UniqueConstraint("tenant_id", "id", name="uq_prospecting_runs_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_prospecting_runs_tenant_idempotency"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "icp_profile_id"],
            ["icp_profiles.tenant_id", "icp_profiles.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("companies_found >= 0", name="companies_found_nonnegative"),
        CheckConstraint("companies_imported >= 0", name="companies_imported_nonnegative"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    icp_profile_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(60), default="manual", nullable=False)
    query: Mapped[str] = mapped_column(String(500), nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[ProspectingRunStatus] = mapped_column(
        Enum(ProspectingRunStatus, name="prospecting_run_status"),
        default=ProspectingRunStatus.QUEUED,
        nullable=False,
    )
    companies_found: Mapped[int] = mapped_column(default=0, nullable=False)
    companies_imported: Mapped[int] = mapped_column(default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(default=3, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Company(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "normalized_domain", name="uq_companies_tenant_domain"),
        UniqueConstraint("tenant_id", "id", name="uq_companies_tenant_id_id"),
        Index("ix_companies_tenant_status", "tenant_id", "status"),
        Index("ix_companies_tenant_name", "tenant_id", "name"),
        CheckConstraint(
            "qualification_score IS NULL OR (qualification_score >= 0 "
            "AND qualification_score <= 100)",
            name="qualification_score_range",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    website_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    address: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)
    employee_count: Mapped[int | None] = mapped_column(nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[CompanyStatus] = mapped_column(
        Enum(CompanyStatus, name="company_status"), default=CompanyStatus.NEW, nullable=False
    )
    qualification_score: Mapped[int | None] = mapped_column(nullable=True)
    qualification_reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    first_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    normalized_name: Mapped[str | None] = mapped_column(String(220), nullable=True)
    website_status: Mapped[str] = mapped_column(String(30), default="unknown", nullable=False)
    website_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    enrichment_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    qualification_confidence: Mapped[float | None] = mapped_column(nullable=True)
    qualification_breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    opportunity_score: Mapped[int | None] = mapped_column(nullable=True)

    decision_makers: Mapped[list["DecisionMaker"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    source_records: Mapped[list["SourceRecord"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class CrawlerJob(UUIDPrimaryKey, TimestampMixin, Base):
    """Durable, tenant-authorized request for one company website verification."""

    __tablename__ = "crawler_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_crawler_jobs_tenant_id_id"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_crawler_jobs_tenant_idempotency"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["companies.tenant_id", "companies.id"],
            ondelete="CASCADE",
        ),
        Index("ix_crawler_jobs_status_created", "status", "created_at"),
        CheckConstraint("attempts >= 0 AND attempts <= 5", name="crawler_attempts_range"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    website_url: Mapped[str] = mapped_column(String(500), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[CrawlerJobStatus] = mapped_column(
        Enum(CrawlerJobStatus, name="crawler_job_status"),
        default=CrawlerJobStatus.QUEUED,
        nullable=False,
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CrawlerJobAttempt(UUIDPrimaryKey, Base):
    """One durable row per claim; never contains the bearer lease token."""

    __tablename__ = "crawler_job_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["crawler_jobs.tenant_id", "crawler_jobs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "job_id", "generation", name="uq_crawler_attempt_generation"),
        CheckConstraint(
            "generation >= 1 AND generation <= 5", name="crawler_attempt_generation_range"
        ),
        CheckConstraint("renewal_count >= 0", name="crawler_attempt_renewals_nonnegative"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    renewal_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(24), default="running", nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(60))


class DecisionMaker(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "decision_makers"
    __table_args__ = (
        Index("ix_decision_makers_tenant_company", "tenant_id", "company_id"),
        Index("ix_decision_makers_tenant_confidence", "tenant_id", "confidence_score"),
        UniqueConstraint("tenant_id", "id", name="uq_decision_makers_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "company_id", "normalized_name", name="uq_decision_maker_company_name"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"], ["companies.tenant_id", "companies.id"], ondelete="CASCADE"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    job_title: Mapped[str | None] = mapped_column(String(160), nullable=True)
    role_category: Mapped[DecisionMakerRole] = mapped_column(
        Enum(DecisionMakerRole, name="decision_maker_role"),
        default=DecisionMakerRole.OTHER,
        nullable=False,
    )
    professional_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    professional_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    profile_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[DecisionMakerStatus] = mapped_column(
        Enum(DecisionMakerStatus, name="decision_maker_status"),
        default=DecisionMakerStatus.UNCONFIRMED,
        nullable=False,
    )
    confidence_score: Mapped[int] = mapped_column(default=0, nullable=False)
    confidence_level: Mapped[str] = mapped_column(
        String(20), default="insufficient", nullable=False
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    company: Mapped[Company] = relationship(back_populates="decision_makers")
    evidence_records: Mapped[list["DecisionMakerEvidence"]] = relationship(
        back_populates="decision_maker", cascade="all, delete-orphan"
    )
    change_records: Mapped[list["DecisionMakerChange"]] = relationship(
        back_populates="decision_maker", cascade="all, delete-orphan"
    )


class DecisionMakerEvidence(UUIDPrimaryKey, Base):
    __tablename__ = "decision_maker_evidence"
    __table_args__ = (
        Index("ix_dm_evidence_tenant_person", "tenant_id", "decision_maker_id"),
        ForeignKeyConstraint(
            ["tenant_id", "decision_maker_id"],
            ["decision_makers.tenant_id", "decision_makers.id"],
            ondelete="CASCADE",
        ),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    decision_maker_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    claim_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    source_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    excerpt: Mapped[str | None] = mapped_column(String(500), nullable=True)
    authority_weight: Mapped[int] = mapped_column(default=50, nullable=False)
    is_current: Mapped[bool] = mapped_column(default=True, nullable=False)
    decision_maker: Mapped[DecisionMaker] = relationship(back_populates="evidence_records")


class DecisionMakerChange(UUIDPrimaryKey, Base):
    __tablename__ = "decision_maker_changes"
    __table_args__ = (
        Index(
            "ix_dm_changes_tenant_person_created", "tenant_id", "decision_maker_id", "created_at"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "decision_maker_id"],
            ["decision_makers.tenant_id", "decision_makers.id"],
            ondelete="CASCADE",
        ),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    decision_maker_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    change_type: Mapped[str] = mapped_column(String(60), nullable=False)
    before: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    after: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source_evidence_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decision_maker: Mapped[DecisionMaker] = relationship(back_populates="change_records")


class SourceRecord(UUIDPrimaryKey, Base):
    __tablename__ = "source_records"
    __table_args__ = (
        Index("ix_source_records_tenant_company", "tenant_id", "company_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"], ["companies.tenant_id", "companies.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "prospecting_run_id"],
            ["prospecting_runs.tenant_id", "prospecting_runs.id"],
            ondelete="RESTRICT",
        ),
        Index("ix_source_records_tenant_captured", "tenant_id", "captured_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    prospecting_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    captured_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    company: Mapped[Company] = relationship(back_populates="source_records")
