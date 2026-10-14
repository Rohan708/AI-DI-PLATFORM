"""Initial metadata store schema (v2): sources, runs, assets/columns with versions,
profiles, relationships, rules, findings.

Revision ID: 0001
Revises:
Create Date: 2026-10-04
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB
ENUM = sa.String(length=32)
VALID_RANGE = "valid_to IS NULL OR valid_to >= valid_from"
CONFIDENCE_RANGE = "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)"


def _ts(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _tenant(table: str) -> tuple[sa.Column[Any], sa.ForeignKeyConstraint]:
    return (
        sa.Column("tenant_id", UUID, nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name=op.f(f"fk_{table}_tenant_id_tenant")
        ),
    )


def _fk(table: str, column: str, ref_table: str, ref_column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{ref_table}.{ref_column}"], name=op.f(f"fk_{table}_{column}_{ref_table}")
    )


def upgrade() -> None:
    op.create_table(
        "tenant",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant")),
        sa.UniqueConstraint("name", name=op.f("uq_tenant_name")),
    )

    op.create_table(
        "data_source",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", ENUM, nullable=False),
        sa.Column("connection_ref", sa.String(length=500), nullable=True),
        sa.Column("settings", JSONB, nullable=False),
        _ts("created_at"),
        _ts("disabled_at", nullable=True),
        *_tenant("data_source"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_source")),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_data_source_tenant_name")),
    )

    op.create_table(
        "ingestion_run",
        sa.Column("id", UUID, nullable=False),
        sa.Column("data_source_id", UUID, nullable=False),
        sa.Column("status", ENUM, nullable=False),
        _ts("started_at"),
        _ts("finished_at", nullable=True),
        sa.Column("stats", JSONB, nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        *_tenant("ingestion_run"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_run")),
        _fk("ingestion_run", "data_source_id", "data_source", "id"),
    )
    op.create_index(
        op.f("ix_ingestion_run_source_started"),
        "ingestion_run",
        ["data_source_id", "started_at"],
    )

    op.create_table(
        "asset",
        sa.Column("asset_key", UUID, nullable=False),
        sa.Column("data_source_id", UUID, nullable=False),
        sa.Column("namespace", sa.String(length=255), nullable=False),
        sa.Column("schema_name", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        _ts("first_seen_at"),
        _ts("last_seen_at"),
        _ts("deleted_at", nullable=True),
        *_tenant("asset"),
        sa.PrimaryKeyConstraint("asset_key", name=op.f("pk_asset")),
        _fk("asset", "data_source_id", "data_source", "id"),
        sa.UniqueConstraint(
            "tenant_id",
            "data_source_id",
            "namespace",
            "schema_name",
            "name",
            name=op.f("uq_asset_natural_key"),
        ),
    )

    op.create_table(
        "asset_version",
        sa.Column("id", UUID, nullable=False),
        sa.Column("asset_key", UUID, nullable=False),
        sa.Column("ingestion_run_id", UUID, nullable=False),
        _ts("valid_from"),
        _ts("valid_to", nullable=True),
        sa.Column("kind", ENUM, nullable=False),
        sa.Column("primary_key", JSONB, nullable=False),
        sa.Column("indexes", JSONB, nullable=False),
        sa.Column("unique_constraints", JSONB, nullable=False),
        sa.Column("foreign_keys", JSONB, nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("properties", JSONB, nullable=False),
        *_tenant("asset_version"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_version")),
        _fk("asset_version", "asset_key", "asset", "asset_key"),
        _fk("asset_version", "ingestion_run_id", "ingestion_run", "id"),
        sa.CheckConstraint(VALID_RANGE, name=op.f("ck_asset_version_valid_range")),
    )
    op.create_index(
        op.f("ux_asset_version_current"),
        "asset_version",
        ["asset_key"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )
    op.create_index(
        op.f("ix_asset_version_asset_valid_from"), "asset_version", ["asset_key", "valid_from"]
    )

    op.create_table(
        "asset_column",
        sa.Column("column_key", UUID, nullable=False),
        sa.Column("asset_key", UUID, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        _ts("first_seen_at"),
        _ts("last_seen_at"),
        _ts("deleted_at", nullable=True),
        *_tenant("asset_column"),
        sa.PrimaryKeyConstraint("column_key", name=op.f("pk_asset_column")),
        _fk("asset_column", "asset_key", "asset", "asset_key"),
        sa.UniqueConstraint("asset_key", "name", name=op.f("uq_asset_column_asset_name")),
    )

    op.create_table(
        "asset_column_version",
        sa.Column("id", UUID, nullable=False),
        sa.Column("column_key", UUID, nullable=False),
        sa.Column("ingestion_run_id", UUID, nullable=False),
        _ts("valid_from"),
        _ts("valid_to", nullable=True),
        sa.Column("native_type", sa.String(length=255), nullable=False),
        sa.Column("type_family", ENUM, nullable=False),
        sa.Column("is_nullable", sa.Boolean(), nullable=False),
        sa.Column("ordinal_position", sa.Integer(), nullable=False),
        sa.Column("default_expr", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        *_tenant("asset_column_version"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_column_version")),
        _fk("asset_column_version", "column_key", "asset_column", "column_key"),
        _fk("asset_column_version", "ingestion_run_id", "ingestion_run", "id"),
        sa.CheckConstraint(VALID_RANGE, name=op.f("ck_asset_column_version_valid_range")),
    )
    op.create_index(
        op.f("ux_asset_column_version_current"),
        "asset_column_version",
        ["column_key"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )
    op.create_index(
        op.f("ix_asset_column_version_column_valid_from"),
        "asset_column_version",
        ["column_key", "valid_from"],
    )

    op.create_table(
        "asset_profile",
        sa.Column("id", UUID, nullable=False),
        sa.Column("asset_key", UUID, nullable=False),
        sa.Column("ingestion_run_id", UUID, nullable=False),
        _ts("measured_at"),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("row_count_is_estimate", sa.Boolean(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        _ts("last_modified_at", nullable=True),
        sa.Column("properties", JSONB, nullable=False),
        *_tenant("asset_profile"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_profile")),
        _fk("asset_profile", "asset_key", "asset", "asset_key"),
        _fk("asset_profile", "ingestion_run_id", "ingestion_run", "id"),
        sa.UniqueConstraint(
            "asset_key", "ingestion_run_id", name=op.f("uq_asset_profile_asset_run")
        ),
    )
    op.create_index(
        op.f("ix_asset_profile_asset_measured"), "asset_profile", ["asset_key", "measured_at"]
    )

    op.create_table(
        "column_profile",
        sa.Column("id", UUID, nullable=False),
        sa.Column("column_key", UUID, nullable=False),
        sa.Column("ingestion_run_id", UUID, nullable=False),
        _ts("measured_at"),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("null_count", sa.BigInteger(), nullable=True),
        sa.Column("null_rate", sa.Double(), nullable=True),
        sa.Column("distinct_count", sa.BigInteger(), nullable=True),
        sa.Column("distinct_is_approx", sa.Boolean(), nullable=False),
        sa.Column("min_repr", sa.Text(), nullable=True),
        sa.Column("max_repr", sa.Text(), nullable=True),
        sa.Column("mean", sa.Double(), nullable=True),
        sa.Column("stddev", sa.Double(), nullable=True),
        sa.Column("avg_length", sa.Double(), nullable=True),
        sa.Column("sample_fraction", sa.Double(), nullable=False),
        sa.Column("top_values", JSONB, nullable=True),
        sa.Column("extra", JSONB, nullable=False),
        *_tenant("column_profile"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_column_profile")),
        _fk("column_profile", "column_key", "asset_column", "column_key"),
        _fk("column_profile", "ingestion_run_id", "ingestion_run", "id"),
        sa.UniqueConstraint(
            "column_key", "ingestion_run_id", name=op.f("uq_column_profile_column_run")
        ),
        sa.CheckConstraint(
            "null_rate IS NULL OR (null_rate >= 0 AND null_rate <= 1)",
            name=op.f("ck_column_profile_null_rate_range"),
        ),
        sa.CheckConstraint(
            "sample_fraction > 0 AND sample_fraction <= 1",
            name=op.f("ck_column_profile_sample_fraction_range"),
        ),
    )
    op.create_index(
        op.f("ix_column_profile_column_measured"),
        "column_profile",
        ["column_key", "measured_at"],
    )

    op.create_table(
        "relationship",
        sa.Column("id", UUID, nullable=False),
        sa.Column("from_asset_key", UUID, nullable=False),
        sa.Column("to_asset_key", UUID, nullable=False),
        sa.Column("kind", ENUM, nullable=False),
        sa.Column("status", ENUM, nullable=False),
        sa.Column("confidence", sa.Double(), nullable=True),
        sa.Column("signals", JSONB, nullable=False),
        sa.Column("evidence", JSONB, nullable=False),
        sa.Column("signature", sa.String(length=64), nullable=False),
        _ts("valid_from"),
        _ts("valid_to", nullable=True),
        sa.Column("reviewed_by", sa.String(length=200), nullable=True),
        _ts("reviewed_at", nullable=True),
        *_tenant("relationship"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_relationship")),
        _fk("relationship", "from_asset_key", "asset", "asset_key"),
        _fk("relationship", "to_asset_key", "asset", "asset_key"),
        sa.CheckConstraint(VALID_RANGE, name=op.f("ck_relationship_valid_range")),
        sa.CheckConstraint(
            "kind <> 'inferred' OR confidence IS NOT NULL",
            name=op.f("ck_relationship_inferred_requires_confidence"),
        ),
        sa.CheckConstraint(CONFIDENCE_RANGE, name=op.f("ck_relationship_confidence_range")),
    )
    op.create_index(
        op.f("ux_relationship_current_signature"),
        "relationship",
        ["tenant_id", "signature"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )
    op.create_index(
        op.f("ix_relationship_from_asset"), "relationship", ["tenant_id", "from_asset_key"]
    )
    op.create_index(op.f("ix_relationship_to_asset"), "relationship", ["tenant_id", "to_asset_key"])

    op.create_table(
        "relationship_column",
        sa.Column("relationship_id", UUID, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("from_column_key", UUID, nullable=False),
        sa.Column("to_column_key", UUID, nullable=False),
        *_tenant("relationship_column"),
        sa.PrimaryKeyConstraint("relationship_id", "ordinal", name=op.f("pk_relationship_column")),
        sa.ForeignKeyConstraint(
            ["relationship_id"],
            ["relationship.id"],
            name=op.f("fk_relationship_column_relationship_id_relationship"),
            ondelete="CASCADE",
        ),
        _fk("relationship_column", "from_column_key", "asset_column", "column_key"),
        _fk("relationship_column", "to_column_key", "asset_column", "column_key"),
    )

    op.create_table(
        "rule",
        sa.Column("id", UUID, nullable=False),
        sa.Column("data_source_id", UUID, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("rule_type", sa.String(length=64), nullable=False),
        sa.Column("asset_key", UUID, nullable=True),
        sa.Column("column_key", UUID, nullable=True),
        sa.Column("definition", JSONB, nullable=False),
        sa.Column("origin", ENUM, nullable=False),
        sa.Column("status", ENUM, nullable=False),
        sa.Column("confidence", sa.Double(), nullable=True),
        sa.Column("evidence", JSONB, nullable=False),
        _ts("created_at"),
        sa.Column("created_by", sa.String(length=200), nullable=True),
        sa.Column("reviewed_by", sa.String(length=200), nullable=True),
        _ts("reviewed_at", nullable=True),
        *_tenant("rule"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rule")),
        _fk("rule", "data_source_id", "data_source", "id"),
        _fk("rule", "asset_key", "asset", "asset_key"),
        _fk("rule", "column_key", "asset_column", "column_key"),
        sa.CheckConstraint(
            "origin <> 'ai' OR confidence IS NOT NULL",
            name=op.f("ck_rule_ai_requires_confidence"),
        ),
        sa.CheckConstraint(CONFIDENCE_RANGE, name=op.f("ck_rule_confidence_range")),
    )
    op.create_index(
        op.f("ix_rule_source_status"), "rule", ["tenant_id", "data_source_id", "status"]
    )

    op.create_table(
        "finding",
        sa.Column("id", UUID, nullable=False),
        sa.Column("data_source_id", UUID, nullable=False),
        sa.Column("category", ENUM, nullable=False),
        sa.Column("check_name", sa.String(length=100), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("severity", ENUM, nullable=False),
        sa.Column("status", ENUM, nullable=False),
        sa.Column("origin", ENUM, nullable=False),
        sa.Column("asset_key", UUID, nullable=True),
        sa.Column("column_key", UUID, nullable=True),
        sa.Column("relationship_id", UUID, nullable=True),
        sa.Column("rule_id", UUID, nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("evidence", JSONB, nullable=False),
        sa.Column("confidence", sa.Double(), nullable=True),
        _ts("first_detected_at"),
        _ts("last_detected_at"),
        sa.Column("first_run_id", UUID, nullable=True),
        sa.Column("last_run_id", UUID, nullable=True),
        _ts("resolved_at", nullable=True),
        _ts("alerted_at", nullable=True),
        sa.Column("reviewed_by", sa.String(length=200), nullable=True),
        _ts("reviewed_at", nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        *_tenant("finding"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_finding")),
        _fk("finding", "data_source_id", "data_source", "id"),
        _fk("finding", "asset_key", "asset", "asset_key"),
        _fk("finding", "column_key", "asset_column", "column_key"),
        _fk("finding", "relationship_id", "relationship", "id"),
        _fk("finding", "rule_id", "rule", "id"),
        _fk("finding", "first_run_id", "ingestion_run", "id"),
        _fk("finding", "last_run_id", "ingestion_run", "id"),
        sa.CheckConstraint(
            "origin <> 'ai' OR confidence IS NOT NULL",
            name=op.f("ck_finding_ai_requires_confidence"),
        ),
        sa.CheckConstraint(CONFIDENCE_RANGE, name=op.f("ck_finding_confidence_range")),
    )
    op.create_index(
        op.f("ux_finding_active_fingerprint"),
        "finding",
        ["tenant_id", "fingerprint"],
        unique=True,
        postgresql_where=sa.text("status IN ('open', 'confirmed')"),
    )
    op.create_index(
        op.f("ix_finding_status_category"), "finding", ["tenant_id", "status", "category"]
    )
    op.create_index(op.f("ix_finding_asset"), "finding", ["tenant_id", "asset_key"])


def downgrade() -> None:
    for table in (
        "finding",
        "rule",
        "relationship_column",
        "relationship",
        "column_profile",
        "asset_profile",
        "asset_column_version",
        "asset_column",
        "asset_version",
        "asset",
        "ingestion_run",
        "data_source",
        "tenant",
    ):
        op.drop_table(table)
