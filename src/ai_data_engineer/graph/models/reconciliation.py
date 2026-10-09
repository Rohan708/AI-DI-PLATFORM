"""Pairs of sources that should hold the same data (Stage 3): an application database
and its warehouse copy, a primary and its replica, last month's system and the new one.

``settings`` maps names when the copy uses other ones (``{"schema_map": {"shop":
"analytics"}, "tables": {"shop.orders": "analytics.fct_orders"}}``) and can override
tolerances. Validated in ``reconcile/settings.py``.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ai_data_engineer.graph.models.base import Base, TenantScoped, utcnow


class ReconciliationPair(TenantScoped, Base):
    __tablename__ = "reconciliation_pair"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))
    left_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_source.id"))
    right_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_source.id"))
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
        CheckConstraint("left_source_id <> right_source_id", name="different_sources"),
    )
