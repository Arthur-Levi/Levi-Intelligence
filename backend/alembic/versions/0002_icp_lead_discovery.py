"""ICP profiles, prospecting runs, company portfolio and evidence provenance.

Revision ID: 0002_prospecting
Revises: 0001_identity
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_prospecting"
down_revision = "0001_identity"
branch_labels = None
depends_on = None

run_status = sa.Enum(
    "QUEUED",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    name="prospecting_run_status",
    create_type=False,
)
company_status = sa.Enum(
    "NEW",
    "QUALIFIED",
    "CONTACTED",
    "CUSTOMER",
    "DISQUALIFIED",
    name="company_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    run_status.create(bind, checkfirst=True)
    company_status.create(bind, checkfirst=True)

    op.create_table(
        "icp_profiles",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("industry", sa.String(120), nullable=False),
        sa.Column("geography", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("company_size", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("criteria", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("exclusions", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_icp_profiles")),
        sa.UniqueConstraint("tenant_id", "name", name="uq_icp_profiles_tenant_name"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_icp_profiles_tenant_id_id"),
    )
    op.create_index("ix_icp_profiles_tenant_active", "icp_profiles", ["tenant_id", "is_active"])

    op.create_table(
        "prospecting_runs",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("icp_profile_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(60), server_default="manual", nullable=False),
        sa.Column("query", sa.String(500), nullable=False),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", run_status, server_default="QUEUED", nullable=False),
        sa.Column("companies_found", sa.Integer(), server_default="0", nullable=False),
        sa.Column("companies_imported", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("companies_found >= 0", name="ck_prospecting_runs_companies_found_nonnegative"),
        sa.CheckConstraint("companies_imported >= 0", name="ck_prospecting_runs_companies_imported_nonnegative"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "icp_profile_id"], ["icp_profiles.tenant_id", "icp_profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prospecting_runs")),
    )
    op.create_index("ix_prospecting_runs_tenant_created", "prospecting_runs", ["tenant_id", "created_at"])

    op.create_table(
        "companies",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("normalized_domain", sa.String(255), nullable=True),
        sa.Column("website_url", sa.String(500), nullable=True),
        sa.Column("phone", sa.String(40), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("address", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("industry", sa.String(120), nullable=True),
        sa.Column("employee_count", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", company_status, server_default="NEW", nullable=False),
        sa.Column("qualification_score", sa.Integer(), nullable=True),
        sa.Column("qualification_reasons", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("first_source", sa.String(100), nullable=True),
        sa.Column("source_url", sa.String(1000), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("qualification_score IS NULL OR (qualification_score >= 0 AND qualification_score <= 100)", name="ck_companies_qualification_score_range"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
        sa.UniqueConstraint("tenant_id", "normalized_domain", name="uq_companies_tenant_domain"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_companies_tenant_id_id"),
    )
    op.create_index("ix_companies_tenant_status", "companies", ["tenant_id", "status"])
    op.create_index("ix_companies_tenant_name", "companies", ["tenant_id", "name"])

    op.create_table(
        "decision_makers",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(160), nullable=False),
        sa.Column("job_title", sa.String(160), nullable=True),
        sa.Column("professional_email", sa.String(320), nullable=True),
        sa.Column("professional_phone", sa.String(40), nullable=True),
        sa.Column("profile_url", sa.String(1000), nullable=True),
        sa.Column("source_url", sa.String(1000), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "company_id"], ["companies.tenant_id", "companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decision_makers")),
    )
    op.create_index("ix_decision_makers_tenant_company", "decision_makers", ["tenant_id", "company_id"])

    op.create_table(
        "source_records",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("prospecting_run_id", sa.Uuid(), nullable=True),
        sa.Column("source_type", sa.String(80), nullable=False),
        sa.Column("source_name", sa.String(160), nullable=False),
        sa.Column("source_url", sa.String(1000), nullable=True),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("captured_data", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "company_id"], ["companies.tenant_id", "companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id", "prospecting_run_id"], ["prospecting_runs.tenant_id", "prospecting_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_records")),
    )
    op.create_index("ix_source_records_tenant_company", "source_records", ["tenant_id", "company_id"])
    op.create_index("ix_source_records_tenant_captured", "source_records", ["tenant_id", "captured_at"])


def downgrade() -> None:
    op.drop_index("ix_source_records_tenant_captured", table_name="source_records")
    op.drop_index("ix_source_records_tenant_company", table_name="source_records")
    op.drop_table("source_records")
    op.drop_index("ix_decision_makers_tenant_company", table_name="decision_makers")
    op.drop_table("decision_makers")
    op.drop_index("ix_companies_tenant_name", table_name="companies")
    op.drop_index("ix_companies_tenant_status", table_name="companies")
    op.drop_table("companies")
    op.drop_index("ix_prospecting_runs_tenant_created", table_name="prospecting_runs")
    op.drop_table("prospecting_runs")
    op.drop_index("ix_icp_profiles_tenant_active", table_name="icp_profiles")
    op.drop_table("icp_profiles")
    company_status.drop(op.get_bind(), checkfirst=True)
    run_status.drop(op.get_bind(), checkfirst=True)
