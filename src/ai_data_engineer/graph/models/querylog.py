"""Remembered query-log evidence.

A database's query statistics (e.g. ``pg_stat_statements``) are wiped when it restarts or
someone resets them. Every join discovery sees is recorded here, so the evidence
"the application joins these columns" survives; ``max_calls`` is the largest call count
ever observed for the pair.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ai_data_engineer.graph.models.base import Base, TenantScoped


class QueryJoin(TenantScoped, Base):
    __tablename__ = "query_join"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    data_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_source.id"))
    # The pair is stored in a canonical order (smaller key first), so A=B and B=A match.
    left_column_key: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_column.column_key"))
    right_column_key: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_column.column_key"))
    max_calls: Mapped[int] = mapped_column(BigInteger)
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]

    __table_args__ = (
        UniqueConstraint(
            "data_source_id", "left_column_key", "right_column_key", name="uq_query_join_pair"
        ),
    )
