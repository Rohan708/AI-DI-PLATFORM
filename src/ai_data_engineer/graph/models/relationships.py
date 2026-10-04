"""Relationships between tables: declared FKs and inferred ones (with confidence).

``from_*`` is the referencing (child) side and ``to_*`` the referenced (parent) side,
as in ``orders.cust_no -> customers.id``. Composite keys use several
``relationship_column`` rows. Relationships are versioned (``valid_from``/``valid_to``)
so we can answer "how were these tables connected before the incident?".
"""

import hashlib
import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ai_data_engineer.graph.models.assets import CURRENT_ROW, VALID_RANGE_CHECK
from ai_data_engineer.graph.models.base import Base, StrEnumType, TenantScoped
from ai_data_engineer.graph.models.enums import RelationshipKind, RelationshipStatus


def relationship_signature(column_pairs: Iterable[tuple[uuid.UUID, uuid.UUID]]) -> str:
    """Stable identity of a relationship: a hash of its (from, to) column pairs.

    Order-insensitive, so the same composite key always produces the same signature.
    """
    canonical = "|".join(sorted(f"{src}>{dst}" for src, dst in column_pairs))
    if not canonical:
        raise ValueError("a relationship needs at least one column pair")
    return hashlib.sha256(canonical.encode()).hexdigest()


class Relationship(TenantScoped, Base):
    __tablename__ = "relationship"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    from_asset_key: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset.asset_key"))
    to_asset_key: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset.asset_key"))
    kind: Mapped[RelationshipKind] = mapped_column(StrEnumType(RelationshipKind))
    status: Mapped[RelationshipStatus] = mapped_column(StrEnumType(RelationshipStatus))
    confidence: Mapped[float | None]  # 0..1; required for inferred relationships
    # Which signals found it, e.g. ["declared"] or ["name_match", "value_inclusion"].
    signals: Mapped[list[str]] = mapped_column(default=list)
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    signature: Mapped[str] = mapped_column(String(64))
    valid_from: Mapped[datetime]
    valid_to: Mapped[datetime | None]
    reviewed_by: Mapped[str | None] = mapped_column(String(200))
    reviewed_at: Mapped[datetime | None]

    columns: Mapped[list["RelationshipColumn"]] = relationship(
        cascade="all, delete-orphan", order_by="RelationshipColumn.ordinal"
    )

    __table_args__ = (
        CheckConstraint(VALID_RANGE_CHECK, name="valid_range"),
        CheckConstraint(
            "kind <> 'inferred' OR confidence IS NOT NULL", name="inferred_requires_confidence"
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
        Index(
            "ux_relationship_current_signature",
            "tenant_id",
            "signature",
            unique=True,
            postgresql_where=CURRENT_ROW,
        ),
        Index("ix_relationship_from_asset", "tenant_id", "from_asset_key"),
        Index("ix_relationship_to_asset", "tenant_id", "to_asset_key"),
    )


class RelationshipColumn(TenantScoped, Base):
    __tablename__ = "relationship_column"

    relationship_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("relationship.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(primary_key=True)
    from_column_key: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_column.column_key"))
    to_column_key: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_column.column_key"))
