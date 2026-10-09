"""History tables added in Stage 1.6.

- ``FindingEvent``: every status change of a finding (who, when, from -> to, note). This is
  the audit trail and, later, the visible accuracy track record ("92% confirmed").
- ``HealthSnapshot``: the health score of each table, schema and the whole source after a
  run, so trends are visible.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ai_data_engineer.graph.models.base import Base, StrEnumType, TenantScoped, utcnow
from ai_data_engineer.graph.models.enums import FindingStatus, HealthScope


class FindingEvent(TenantScoped, Base):
    __tablename__ = "finding_event"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finding.id", ondelete="CASCADE"))
    at: Mapped[datetime] = mapped_column(default=utcnow)
    actor: Mapped[str] = mapped_column(String(200))  # a person, or "aide" for automatic changes
    from_status: Mapped[FindingStatus | None] = mapped_column(StrEnumType(FindingStatus))
    to_status: Mapped[FindingStatus] = mapped_column(StrEnumType(FindingStatus))
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_finding_event_finding_at", "finding_id", "at"),)


class HealthSnapshot(TenantScoped, Base):
    __tablename__ = "health_snapshot"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    data_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_source.id"))
    ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ingestion_run.id"))
    computed_at: Mapped[datetime]
    scope: Mapped[HealthScope] = mapped_column(StrEnumType(HealthScope))
    asset_key: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset.asset_key"))
    schema_name: Mapped[str | None] = mapped_column(String(255))
    score: Mapped[float]  # 0..100
    min_child_score: Mapped[float | None]  # worst table, for schema/source scopes
    # Open findings counted, by severity: {"high": 2, "medium": 1, ...}
    open_findings: Mapped[dict[str, Any]] = mapped_column(default=dict)

    __table_args__ = (Index("ix_health_snapshot_source_computed", "data_source_id", "computed_at"),)
