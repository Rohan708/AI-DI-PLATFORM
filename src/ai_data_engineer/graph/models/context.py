"""What people (or their dbt project) tell us about a source (Stage 2.7).

- ``ContextNote``: one fact about the source, a table or a column: a description, how
  critical a table is, the business's closed weekdays, a freshness deadline. Replaced
  facts are kept (``valid_to`` set), so we know what was believed when.
- ``QuestionAnswer``: the answer to one generated question (``aide ask``), so the same
  question isn't asked again; "don't know" answers come back after a while.

Expectations that become checks (required, unique, allowed values) are stored as rules
(``rule`` table, origin ``user``), so they run and are reviewed like every other rule.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ai_data_engineer.graph.models.assets import CURRENT_ROW
from ai_data_engineer.graph.models.base import Base, StrEnumType, TenantScoped, utcnow
from ai_data_engineer.graph.models.enums import Origin


class ContextNote(TenantScoped, Base):
    __tablename__ = "context_note"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    data_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_source.id"))
    asset_key: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset.asset_key"))
    column_key: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset_column.column_key"))
    # description | criticality | closed_weekdays | freshness (validated in context/store.py)
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    origin: Mapped[Origin] = mapped_column(StrEnumType(Origin))
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    valid_to: Mapped[datetime | None]

    __table_args__ = (
        Index("ix_context_note_source_key", "data_source_id", "key"),
        # One current fact per subject and key. NULL subjects (the source itself, or a
        # table rather than a column) count as equal, so they're covered too (Postgres 15+).
        Index(
            "ux_context_note_current",
            "data_source_id",
            "asset_key",
            "column_key",
            "key",
            unique=True,
            postgresql_where=CURRENT_ROW,
            postgresql_nulls_not_distinct=True,
        ),
    )


class QuestionAnswer(TenantScoped, Base):
    __tablename__ = "question_answer"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    data_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_source.id"))
    question_key: Mapped[str] = mapped_column(String(255))  # e.g. "unique:<column key>"
    answer: Mapped[dict[str, Any]] = mapped_column(default=dict)  # {"answer": ...}
    answered_by: Mapped[str] = mapped_column(String(200))
    answered_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (UniqueConstraint("data_source_id", "question_key"),)
