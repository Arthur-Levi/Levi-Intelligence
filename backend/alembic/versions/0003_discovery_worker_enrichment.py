"""Durable discovery jobs, enrichment and ICP qualification.

Revision ID: 0003_discovery_enrichment
Revises: 0002_prospecting
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_discovery_enrichment"
down_revision = "0002_prospecting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prospecting_runs", sa.Column("idempotency_key", sa.String(128), nullable=True))
    op.add_column("prospecting_runs", sa.Column("request_fingerprint", sa.String(64), nullable=True))
    op.add_column("prospecting_runs", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("prospecting_runs", sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False))
    op.add_column("prospecting_runs", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("prospecting_runs", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("prospecting_runs", sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("prospecting_runs", sa.Column("worker_job_id", sa.String(128), nullable=True))
    op.create_unique_constraint("uq_prospecting_runs_tenant_idempotency", "prospecting_runs", ["tenant_id", "idempotency_key"])
    op.create_index("ix_prospecting_runs_status_retry", "prospecting_runs", ["status", "next_retry_at"])

    op.add_column("companies", sa.Column("normalized_name", sa.String(220), nullable=True))
    op.add_column("companies", sa.Column("website_status", sa.String(30), server_default="unknown", nullable=False))
    op.add_column("companies", sa.Column("website_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("companies", sa.Column("enrichment_status", sa.String(30), server_default="pending", nullable=False))
    op.add_column("companies", sa.Column("qualification_confidence", sa.Float(), nullable=True))
    op.add_column("companies", sa.Column("qualification_breakdown", sa.JSON(), server_default=sa.text("'{}'"), nullable=False))
    op.add_column("companies", sa.Column("opportunity_score", sa.Integer(), nullable=True))
    op.create_index("ix_companies_tenant_normalized_name", "companies", ["tenant_id", "normalized_name"])


def downgrade() -> None:
    op.drop_index("ix_companies_tenant_normalized_name", table_name="companies")
    op.drop_column("companies", "opportunity_score")
    op.drop_column("companies", "qualification_breakdown")
    op.drop_column("companies", "qualification_confidence")
    op.drop_column("companies", "enrichment_status")
    op.drop_column("companies", "website_checked_at")
    op.drop_column("companies", "website_status")
    op.drop_column("companies", "normalized_name")
    op.drop_index("ix_prospecting_runs_status_retry", table_name="prospecting_runs")
    op.drop_constraint("uq_prospecting_runs_tenant_idempotency", "prospecting_runs", type_="unique")
    op.drop_column("prospecting_runs", "worker_job_id")
    op.drop_column("prospecting_runs", "next_retry_at")
    op.drop_column("prospecting_runs", "cancel_requested_at")
    op.drop_column("prospecting_runs", "last_error")
    op.drop_column("prospecting_runs", "max_attempts")
    op.drop_column("prospecting_runs", "attempts")
    op.drop_column("prospecting_runs", "request_fingerprint")
    op.drop_column("prospecting_runs", "idempotency_key")
