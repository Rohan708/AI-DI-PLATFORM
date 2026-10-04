"""Explicit expectations about the data, e.g. ``ship_date >= order_date``.

Rules can come from the system, a user, or AI. AI-proposed rules start as ``proposed``
and only run after a human sets them ``active``; the DB enforces that AI rules carry
a confidence.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from ai_data_engineer.graph.models.base import Base, StrEnumType, TenantScoped, utcnow
from ai_data_engineer.graph.models.enums import Origin, RuleStatus


class Rule(TenantScoped, Base):
    __tablename__ = "rule"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    data_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_source.id"))
    name: Mapped[str] = mapped_column(String(255))
    # Open-ended on purpose ("not_null", "range", "sql_assertion", ...); validated in code.
    rule_type: Mapped[str] = mapped_column(String(64))
    asset_key: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset.asset_key"))
    column_key: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset_column.column_key"))
    definition: Mapped[dict[str, Any]] = mapped_column(default=dict)
    origin: Mapped[Origin] = mapped_column(StrEnumType(Origin))
    status: Mapped[RuleStatus] = mapped_column(StrEnumType(RuleStatus))
    confidence: Mapped[float | None]
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(200))
    reviewed_by: Mapped[str | None] = mapped_column(String(200))
    reviewed_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("origin <> 'ai' OR confidence IS NOT NULL", name="ai_requires_confidence"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
        Index("ix_rule_source_status", "tenant_id", "data_source_id", "status"),
    )
