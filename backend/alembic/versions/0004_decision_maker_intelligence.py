"""Decision maker intelligence, provenance and change history.

Revision ID: 0004_decision_maker_intelligence
Revises: 0003_discovery_enrichment
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_decision_maker_intelligence"
down_revision = "0003_discovery_enrichment"
branch_labels = None
depends_on = None

role_enum = sa.Enum(
    "OWNER",
    "PARTNER",
    "DIRECTOR",
    "MANAGER",
    "PROCUREMENT",
    "MARKETING",
    "OTHER",
    name="decision_maker_role",
    create_type=False,
)
status_enum = sa.Enum(
    "CURRENT",
    "UNCONFIRMED",
    "FORMER",
    "DISPUTED",
    name="decision_maker_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    role_enum.create(bind, checkfirst=True)
    status_enum.create(bind, checkfirst=True)
    op.add_column("decision_makers", sa.Column("normalized_name", sa.String(180), nullable=True))
    op.add_column("decision_makers", sa.Column("role_category", role_enum, server_default="OTHER", nullable=False))
    op.add_column("decision_makers", sa.Column("status", status_enum, server_default="UNCONFIRMED", nullable=False))
    op.add_column("decision_makers", sa.Column("confidence_score", sa.Integer(), server_default="0", nullable=False))
    op.add_column("decision_makers", sa.Column("confidence_level", sa.String(20), server_default="insufficient", nullable=False))
    op.add_column("decision_makers", sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.add_column("decision_makers", sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE decision_makers SET normalized_name = lower(trim(full_name)) WHERE normalized_name IS NULL")
    op.alter_column("decision_makers", "normalized_name", nullable=False)
    op.create_unique_constraint("uq_decision_maker_company_name", "decision_makers", ["tenant_id", "company_id", "normalized_name"])
    op.create_index("ix_decision_makers_tenant_confidence", "decision_makers", ["tenant_id", "confidence_score"])

    op.create_table(
        "decision_maker_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("decision_maker_id", sa.Uuid(), nullable=False),
        sa.Column("claim_type", sa.String(40), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_name", sa.String(160), nullable=False),
        sa.Column("source_url", sa.String(1000), nullable=False),
        sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("excerpt", sa.String(500), nullable=True),
        sa.Column("authority_weight", sa.Integer(), server_default="50", nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id", "decision_maker_id"], ["decision_makers.tenant_id", "decision_makers.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_dm_evidence_tenant_person", "decision_maker_evidence", ["tenant_id", "decision_maker_id"])

    op.create_table(
        "decision_maker_changes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("decision_maker_id", sa.Uuid(), nullable=False),
        sa.Column("change_type", sa.String(60), nullable=False),
        sa.Column("before", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("after", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("source_evidence_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id", "decision_maker_id"], ["decision_makers.tenant_id", "decision_makers.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_dm_changes_tenant_person_created", "decision_maker_changes", ["tenant_id", "decision_maker_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_dm_changes_tenant_person_created", table_name="decision_maker_changes")
    op.drop_table("decision_maker_changes")
    op.drop_index("ix_dm_evidence_tenant_person", table_name="decision_maker_evidence")
    op.drop_table("decision_maker_evidence")
    op.drop_index("ix_decision_makers_tenant_confidence", table_name="decision_makers")
    op.drop_constraint("uq_decision_maker_company_name", "decision_makers", type_="unique")
    for column in ("last_verified_at", "first_seen_at", "confidence_level", "confidence_score", "status", "role_category", "normalized_name"):
        op.drop_column("decision_makers", column)
    status_enum.drop(op.get_bind(), checkfirst=True)
    role_enum.drop(op.get_bind(), checkfirst=True)
