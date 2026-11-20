"""Add durable crawler worker leases and bounded retry state.

Revision ID: 0006_crawler_worker_leases
Revises: 0005_crawler_execution_jobs
"""

import sqlalchemy as sa

from alembic import op

revision = "0006_crawler_worker_leases"
down_revision = "0005_crawler_execution_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "crawler_jobs", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("crawler_jobs", sa.Column("lease_token", sa.Uuid(), nullable=True))
    op.add_column(
        "crawler_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "crawler_jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "crawler_jobs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "crawler_attempts_range", "crawler_jobs", "attempts >= 0 AND attempts <= 5"
    )
    op.create_index(
        "ix_crawler_jobs_claim", "crawler_jobs", ["status", "next_attempt_at", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_crawler_jobs_claim", table_name="crawler_jobs")
    op.drop_constraint("crawler_attempts_range", "crawler_jobs", type_="check")
    for column in (
        "next_attempt_at",
        "heartbeat_at",
        "lease_expires_at",
        "lease_token",
        "attempts",
    ):
        op.drop_column("crawler_jobs", column)
