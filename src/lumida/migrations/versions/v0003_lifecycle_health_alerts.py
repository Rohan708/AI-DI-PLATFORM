"""Findings audit trail, health score history, alert settings + quiet baseline on sources.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("data_source", sa.Column("alert_webhook_ref", sa.String(500), nullable=True))
    op.add_column("data_source", sa.Column("baseline_completed_at", TS, nullable=True))

    op.create_table(
        "finding_event",
        sa.Column("id", UUID, nullable=False),
        sa.Column("finding_id", UUID, nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_finding_event")),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["finding.id"],
            name=op.f("fk_finding_event_finding_id_finding"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name=op.f("fk_finding_event_tenant_id_tenant")
        ),
    )
    op.create_index(op.f("ix_finding_event_finding_at"), "finding_event", ["finding_id", "at"])

    op.create_table(
        "health_snapshot",
        sa.Column("id", UUID, nullable=False),
        sa.Column("data_source_id", UUID, nullable=False),
        sa.Column("ingestion_run_id", UUID, nullable=True),
        sa.Column("computed_at", TS, nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("asset_key", UUID, nullable=True),
        sa.Column("schema_name", sa.String(255), nullable=True),
        sa.Column("score", sa.Double(), nullable=False),
        sa.Column("min_child_score", sa.Double(), nullable=True),
        sa.Column("open_findings", postgresql.JSONB, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_health_snapshot")),
        sa.ForeignKeyConstraint(
            ["data_source_id"],
            ["data_source.id"],
            name=op.f("fk_health_snapshot_data_source_id_data_source"),
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_run_id"],
            ["ingestion_run.id"],
            name=op.f("fk_health_snapshot_ingestion_run_id_ingestion_run"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_key"], ["asset.asset_key"], name=op.f("fk_health_snapshot_asset_key_asset")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name=op.f("fk_health_snapshot_tenant_id_tenant")
        ),
    )
    op.create_index(
        op.f("ix_health_snapshot_source_computed"),
        "health_snapshot",
        ["data_source_id", "computed_at"],
    )


def downgrade() -> None:
    op.drop_table("health_snapshot")
    op.drop_table("finding_event")
    op.drop_column("data_source", "baseline_completed_at")
    op.drop_column("data_source", "alert_webhook_ref")
