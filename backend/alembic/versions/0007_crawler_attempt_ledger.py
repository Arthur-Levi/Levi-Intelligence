"""Add tenant-scoped, bounded crawler attempt ledger.

Revision ID: 0007_crawler_attempt_ledger
Revises: 0006_crawler_worker_leases
"""

import sqlalchemy as sa

from alembic import op

revision = "0007_crawler_attempt_ledger"
down_revision = "0006_crawler_worker_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crawler_job_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Uuid(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("renewal_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(24), server_default="running", nullable=False),
        sa.Column("failure_code", sa.String(60), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["crawler_jobs.tenant_id", "crawler_jobs.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "job_id", "generation", name="uq_crawler_attempt_generation"),
        sa.CheckConstraint("generation >= 1 AND generation <= 5", name="crawler_attempt_generation_range"),
        sa.CheckConstraint("renewal_count >= 0", name="crawler_attempt_renewals_nonnegative"),
    )
    # Existing jobs remain runnable. Legacy attempts have no worker identity or ledger row;
    # every claim after this migration writes an attempt transactionally.


def downgrade() -> None:
    op.drop_table("crawler_job_attempts")
