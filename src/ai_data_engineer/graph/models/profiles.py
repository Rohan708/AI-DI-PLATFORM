"""Measurement time series: one row per asset/column per ingestion run.

These are what statistical detection reads ("is today's null rate normal for this
column?"). Measurements are computed inside the customer database; only aggregates
are stored here.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ai_data_engineer.graph.models.base import Base, TenantScoped


class AssetProfile(TenantScoped, Base):
    __tablename__ = "asset_profile"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    asset_key: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset.asset_key"))
    ingestion_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_run.id"))
    measured_at: Mapped[datetime]

    row_count: Mapped[int | None] = mapped_column(BigInteger)
    row_count_is_estimate: Mapped[bool] = mapped_column(default=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # When the table's data last changed, if the database can tell us (freshness).
    last_modified_at: Mapped[datetime | None]
    properties: Mapped[dict[str, Any]] = mapped_column(default=dict)

    __table_args__ = (
        UniqueConstraint("asset_key", "ingestion_run_id", name="uq_asset_profile_asset_run"),
        Index("ix_asset_profile_asset_measured", "asset_key", "measured_at"),
    )


class ColumnProfile(TenantScoped, Base):
    __tablename__ = "column_profile"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    column_key: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_column.column_key"))
    ingestion_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_run.id"))
    measured_at: Mapped[datetime]

    row_count: Mapped[int | None] = mapped_column(BigInteger)
    null_count: Mapped[int | None] = mapped_column(BigInteger)
    null_rate: Mapped[float | None]  # 0..1
    distinct_count: Mapped[int | None] = mapped_column(BigInteger)
    distinct_is_approx: Mapped[bool] = mapped_column(default=False)
    # Text representations of the extremes, not typed raw values.
    min_repr: Mapped[str | None] = mapped_column(Text)
    max_repr: Mapped[str | None] = mapped_column(Text)
    mean: Mapped[float | None]
    stddev: Mapped[float | None]
    avg_length: Mapped[float | None]
    # Fraction of rows the measurement was computed on (1.0 = full scan).
    sample_fraction: Mapped[float] = mapped_column(default=1.0)
    # [{"value": ..., "count": ...}] for low-cardinality columns, only when the source's
    # settings allow storing values. NULL = not collected.
    top_values: Mapped[list[dict[str, Any]] | None]
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)

    __table_args__ = (
        UniqueConstraint("column_key", "ingestion_run_id", name="uq_column_profile_column_run"),
        Index("ix_column_profile_column_measured", "column_key", "measured_at"),
        CheckConstraint(
            "null_rate IS NULL OR (null_rate >= 0 AND null_rate <= 1)", name="null_rate_range"
        ),
        CheckConstraint(
            "sample_fraction > 0 AND sample_fraction <= 1", name="sample_fraction_range"
        ),
    )
