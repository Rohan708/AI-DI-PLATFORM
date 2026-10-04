"""Findings: the single output table for every detector (deterministic or AI).

Dedup: at most one *active* (open/confirmed) finding per ``fingerprint``. A detector
that sees the same problem again updates ``last_detected_at`` instead of inserting.
Deterministic findings have ``confidence = NULL``; AI findings must carry one (DB-enforced).
"""

import hashlib
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from ai_data_engineer.graph.models.base import Base, StrEnumType, TenantScoped, utcnow
from ai_data_engineer.graph.models.enums import (
    ACTIVE_FINDING_STATUSES,
    FindingCategory,
    FindingStatus,
    Origin,
    Severity,
)


def finding_fingerprint(check_name: str, *subject: object) -> str:
    """Stable identity of "this check, about this thing", e.g.
    ``finding_fingerprint("null_rate_spike", column_key)``."""
    raw = "|".join([check_name, *(str(part) for part in subject)])
    return hashlib.sha256(raw.encode()).hexdigest()


_ACTIVE = ", ".join(f"'{status.value}'" for status in ACTIVE_FINDING_STATUSES)


class Finding(TenantScoped, Base):
    __tablename__ = "finding"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    data_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_source.id"))
    category: Mapped[FindingCategory] = mapped_column(StrEnumType(FindingCategory))
    check_name: Mapped[str] = mapped_column(String(100))
    fingerprint: Mapped[str] = mapped_column(String(64))
    severity: Mapped[Severity] = mapped_column(StrEnumType(Severity))
    status: Mapped[FindingStatus] = mapped_column(
        StrEnumType(FindingStatus), default=FindingStatus.OPEN
    )
    origin: Mapped[Origin] = mapped_column(StrEnumType(Origin), default=Origin.SYSTEM)

    # What the finding is about; any combination may be set.
    asset_key: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset.asset_key"))
    column_key: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset_column.column_key"))
    relationship_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("relationship.id"))
    rule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("rule.id"))

    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text)
    # Baseline, observed value, threshold, queries used — enough for a human to verify.
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    confidence: Mapped[float | None]

    first_detected_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_detected_at: Mapped[datetime] = mapped_column(default=utcnow)
    first_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ingestion_run.id"))
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ingestion_run.id"))
    resolved_at: Mapped[datetime | None]
    alerted_at: Mapped[datetime | None]
    reviewed_by: Mapped[str | None] = mapped_column(String(200))
    reviewed_at: Mapped[datetime | None]
    review_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("origin <> 'ai' OR confidence IS NOT NULL", name="ai_requires_confidence"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
        Index(
            "ux_finding_active_fingerprint",
            "tenant_id",
            "fingerprint",
            unique=True,
            postgresql_where=text(f"status IN ({_ACTIVE})"),
        ),
        Index("ix_finding_status_category", "tenant_id", "status", "category"),
        Index("ix_finding_asset", "tenant_id", "asset_key"),
    )
