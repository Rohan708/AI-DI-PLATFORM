"""Remembered query-log join evidence (survives query-statistics resets).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "query_join",
        sa.Column("id", UUID, nullable=False),
        sa.Column("data_source_id", UUID, nullable=False),
        sa.Column("left_column_key", UUID, nullable=False),
        sa.Column("right_column_key", UUID, nullable=False),
        sa.Column("max_calls", sa.BigInteger(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_query_join")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name=op.f("fk_query_join_tenant_id_tenant")
        ),
        sa.ForeignKeyConstraint(
            ["data_source_id"],
            ["data_source.id"],
            name=op.f("fk_query_join_data_source_id_data_source"),
        ),
        sa.ForeignKeyConstraint(
            ["left_column_key"],
            ["asset_column.column_key"],
            name=op.f("fk_query_join_left_column_key_asset_column"),
        ),
        sa.ForeignKeyConstraint(
            ["right_column_key"],
            ["asset_column.column_key"],
            name=op.f("fk_query_join_right_column_key_asset_column"),
        ),
        sa.UniqueConstraint(
            "data_source_id",
            "left_column_key",
            "right_column_key",
            name=op.f("uq_query_join_pair"),
        ),
    )


def downgrade() -> None:
    op.drop_table("query_join")
