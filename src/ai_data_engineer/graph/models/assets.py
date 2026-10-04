"""Tables/views and their columns: stable identity rows + versioned structure rows.

- ``Asset`` / ``AssetColumn`` are identities. Their keys never change, so relationships,
  rules and findings that point at them never break. Only bookkeeping fields
  (``last_seen_at``, ``deleted_at``) are updated in place.
- ``AssetVersion`` / ``AssetColumnVersion`` record structure over time. A new row is
  inserted on change and the previous row's ``valid_to`` is closed; ``valid_to IS NULL``
  means current. At most one current version exists per identity (partial unique index).

Write these only through ``ai_data_engineer.graph.versioning``.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from ai_data_engineer.graph.models.base import Base, StrEnumType, TenantScoped
from ai_data_engineer.graph.models.enums import AssetKind, TypeFamily

VALID_RANGE_CHECK = "valid_to IS NULL OR valid_to >= valid_from"
CURRENT_ROW = text("valid_to IS NULL")


class Asset(TenantScoped, Base):
    """Identity of a table or view. ``namespace`` is the database/catalog name."""

    __tablename__ = "asset"

    asset_key: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    data_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_source.id"))
    namespace: Mapped[str] = mapped_column(String(255))
    schema_name: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    # Set when a scan no longer finds the table; cleared if it reappears (same identity).
    deleted_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "data_source_id",
            "namespace",
            "schema_name",
            "name",
            name="uq_asset_natural_key",
        ),
    )


class AssetVersion(TenantScoped, Base):
    """Structure of a table at a point in time: kind, keys, indexes, declared FKs."""

    __tablename__ = "asset_version"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    asset_key: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset.asset_key"))
    ingestion_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_run.id"))
    valid_from: Mapped[datetime]
    valid_to: Mapped[datetime | None]

    kind: Mapped[AssetKind] = mapped_column(StrEnumType(AssetKind))
    primary_key: Mapped[list[str]] = mapped_column(default=list)  # empty = no PK
    indexes: Mapped[list[dict[str, Any]]] = mapped_column(default=list)
    unique_constraints: Mapped[list[dict[str, Any]]] = mapped_column(default=list)
    # Declared FK constraints as reported by the database. Relationship discovery
    # (Stage 1.4) turns these into ``relationship`` rows.
    foreign_keys: Mapped[list[dict[str, Any]]] = mapped_column(default=list)
    comment: Mapped[str | None] = mapped_column(Text)
    properties: Mapped[dict[str, Any]] = mapped_column(default=dict)

    __table_args__ = (
        CheckConstraint(VALID_RANGE_CHECK, name="valid_range"),
        Index("ux_asset_version_current", "asset_key", unique=True, postgresql_where=CURRENT_ROW),
        Index("ix_asset_version_asset_valid_from", "asset_key", "valid_from"),
    )


class AssetColumn(TenantScoped, Base):
    """Identity of a column within an asset."""

    __tablename__ = "asset_column"

    column_key: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    asset_key: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset.asset_key"))
    name: Mapped[str] = mapped_column(String(255))
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]

    __table_args__ = (UniqueConstraint("asset_key", "name", name="uq_asset_column_asset_name"),)


class AssetColumnVersion(TenantScoped, Base):
    """Structure of a column at a point in time. Type and schema drift are diffs here."""

    __tablename__ = "asset_column_version"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    column_key: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_column.column_key"))
    ingestion_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_run.id"))
    valid_from: Mapped[datetime]
    valid_to: Mapped[datetime | None]

    native_type: Mapped[str] = mapped_column(String(255))  # e.g. "character varying(255)"
    type_family: Mapped[TypeFamily] = mapped_column(StrEnumType(TypeFamily))
    is_nullable: Mapped[bool]
    ordinal_position: Mapped[int]
    default_expr: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(VALID_RANGE_CHECK, name="valid_range"),
        Index(
            "ux_asset_column_version_current",
            "column_key",
            unique=True,
            postgresql_where=CURRENT_ROW,
        ),
        Index("ix_asset_column_version_column_valid_from", "column_key", "valid_from"),
    )
