"""Reconciliation pairs: two sources that should hold the same data (Stage 3).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "reconciliation_pair",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("left_source_id", UUID, nullable=False),
        sa.Column("right_source_id", UUID, nullable=False),
        sa.Column("settings", postgresql.JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_pair")),
        sa.ForeignKeyConstraint(
            ["left_source_id"],
            ["data_source.id"],
            name=op.f("fk_reconciliation_pair_left_source_id_data_source"),
        ),
        sa.ForeignKeyConstraint(
            ["right_source_id"],
            ["data_source.id"],
            name=op.f("fk_reconciliation_pair_right_source_id_data_source"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name=op.f("fk_reconciliation_pair_tenant_id_tenant")
        ),
        sa.UniqueConstraint(
            "tenant_id", "name", name=op.f("uq_reconciliation_pair_tenant_id_name")
        ),
        sa.CheckConstraint(
            "left_source_id <> right_source_id",
            name=op.f("ck_reconciliation_pair_different_sources"),
        ),
    )


def downgrade() -> None:
    op.drop_table("reconciliation_pair")
