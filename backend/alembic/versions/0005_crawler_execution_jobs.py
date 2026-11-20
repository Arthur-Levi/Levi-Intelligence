"""Add tenant-scoped crawler execution jobs.

Revision ID: 0005_crawler_execution_jobs
Revises: 0004_decision_maker_intelligence
"""
import sqlalchemy as sa

from alembic import op

revision = "0005_crawler_execution_jobs"
down_revision = "0004_decision_maker_intelligence"
branch_labels = None
depends_on = None

crawler_job_status = sa.Enum(
    "QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", name="crawler_job_status"
)


def upgrade() -> None:
    crawler_job_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "crawler_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("website_url", sa.String(500), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", crawler_job_status, nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("failure_code", sa.String(60), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id"], ["companies.tenant_id", "companies.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_crawler_jobs_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_crawler_jobs_tenant_idempotency"
        ),
    )
    op.create_index("ix_crawler_jobs_status_created", "crawler_jobs", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_crawler_jobs_status_created", table_name="crawler_jobs")
    op.drop_table("crawler_jobs")
    crawler_job_status.drop(op.get_bind(), checkfirst=True)
