"""Context from people and dbt: notes about sources/tables/columns, answered questions.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "context_note",
        sa.Column("id", UUID, nullable=False),
        sa.Column("data_source_id", UUID, nullable=False),
        sa.Column("asset_key", UUID, nullable=True),
        sa.Column("column_key", UUID, nullable=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", postgresql.JSONB, nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("valid_to", TS, nullable=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_note")),
        sa.ForeignKeyConstraint(
            ["data_source_id"],
            ["data_source.id"],
            name=op.f("fk_context_note_data_source_id_data_source"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_key"], ["asset.asset_key"], name=op.f("fk_context_note_asset_key_asset")
        ),
        sa.ForeignKeyConstraint(
            ["column_key"],
            ["asset_column.column_key"],
            name=op.f("fk_context_note_column_key_asset_column"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name=op.f("fk_context_note_tenant_id_tenant")
        ),
    )
    op.create_index(op.f("ix_context_note_source_key"), "context_note", ["data_source_id", "key"])
    op.create_index(
        "ux_context_note_current",
        "context_note",
        ["data_source_id", "asset_key", "column_key", "key"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
        postgresql_nulls_not_distinct=True,
    )
    op.create_table(
        "question_answer",
        sa.Column("id", UUID, nullable=False),
        sa.Column("data_source_id", UUID, nullable=False),
        sa.Column("question_key", sa.String(255), nullable=False),
        sa.Column("answer", postgresql.JSONB, nullable=False),
        sa.Column("answered_by", sa.String(200), nullable=False),
        sa.Column("answered_at", TS, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_question_answer")),
        sa.ForeignKeyConstraint(
            ["data_source_id"],
            ["data_source.id"],
            name=op.f("fk_question_answer_data_source_id_data_source"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name=op.f("fk_question_answer_tenant_id_tenant")
        ),
        sa.UniqueConstraint(
            "data_source_id",
            "question_key",
            name=op.f("uq_question_answer_data_source_id_question_key"),
        ),
    )


def downgrade() -> None:
    op.drop_table("question_answer")
    op.drop_table("context_note")
