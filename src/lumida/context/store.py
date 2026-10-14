"""Reading and writing what people told us about a source (``ContextNote``).

Keys (one current value per subject and key; replaced values keep their history):

| key | subject | value |
|---|---|---|
| ``description`` | source, table or column | ``{"text": "..."}`` |
| ``criticality`` | table | ``{"level": 1 | 2 | 3}`` (low / normal / critical) |
| ``closed_weekdays`` | source | ``{"days": [5, 6]}`` (Monday = 0) |
| ``freshness`` | table | ``{"column": "loaded_at", "max_age_hours": 26}`` |
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lumida.graph.models import ContextNote, DataSource, Origin

NOTE_KEYS = frozenset({"description", "criticality", "closed_weekdays", "freshness"})
CRITICALITY_LEVELS = {1: "low", 2: "normal", 3: "critical"}
DEFAULT_CRITICALITY = 2
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ContextError(ValueError):
    pass


@dataclass
class SourceContext:
    """Everything people told us about one source, ready for the checks to use."""

    descriptions: dict[tuple[uuid.UUID | None, uuid.UUID | None], str] = field(
        default_factory=dict
    )  # (asset_key, column_key) -> text; (None, None) is the source itself
    criticality: dict[uuid.UUID, int] = field(default_factory=dict)  # asset_key -> 1..3
    closed_weekdays: frozenset[int] = frozenset()
    freshness: dict[uuid.UUID, tuple[str, float]] = field(default_factory=dict)  # column, hours

    def level(self, asset_key: uuid.UUID | None) -> int:
        if asset_key is None:
            return DEFAULT_CRITICALITY
        return self.criticality.get(asset_key, DEFAULT_CRITICALITY)


def set_note(
    session: Session,
    source: DataSource,
    key: str,
    value: dict[str, Any],
    *,
    by: str,
    now: datetime,
    origin: Origin = Origin.USER,
    asset_key: uuid.UUID | None = None,
    column_key: uuid.UUID | None = None,
) -> ContextNote:
    """Record a fact, replacing the current one for the same subject and key."""
    if key not in NOTE_KEYS:
        raise ContextError(f"unknown context key {key!r} (known: {', '.join(sorted(NOTE_KEYS))})")
    same_asset = (
        ContextNote.asset_key.is_(None) if asset_key is None else ContextNote.asset_key == asset_key
    )
    same_column = (
        ContextNote.column_key.is_(None)
        if column_key is None
        else ContextNote.column_key == column_key
    )
    current = select(ContextNote).where(
        ContextNote.data_source_id == source.id,
        ContextNote.key == key,
        same_asset,
        same_column,
        ContextNote.valid_to.is_(None),
    )
    for old in session.scalars(current):
        old.valid_to = now
    session.flush()  # close the old fact before the new one exists (one current fact)
    note = ContextNote(
        tenant_id=source.tenant_id,
        data_source_id=source.id,
        asset_key=asset_key,
        column_key=column_key,
        key=key,
        value=value,
        origin=origin,
        created_by=by,
        created_at=now,
    )
    session.add(note)
    session.flush()
    return note


def current_notes(session: Session, source: DataSource) -> list[ContextNote]:
    return list(
        session.scalars(
            select(ContextNote)
            .where(ContextNote.data_source_id == source.id, ContextNote.valid_to.is_(None))
            .order_by(ContextNote.created_at)
        )
    )


def load_context(session: Session, source: DataSource) -> SourceContext:
    ctx = SourceContext()
    for note in current_notes(session, source):
        v = note.value
        if note.key == "description" and v.get("text"):
            ctx.descriptions[(note.asset_key, note.column_key)] = str(v["text"])
        elif note.key == "criticality" and note.asset_key is not None:
            ctx.criticality[note.asset_key] = int(v.get("level", DEFAULT_CRITICALITY))
        elif note.key == "closed_weekdays" and note.asset_key is None:
            ctx.closed_weekdays = frozenset(int(d) for d in v.get("days", []))
        elif note.key == "freshness" and note.asset_key is not None and v.get("column"):
            ctx.freshness[note.asset_key] = (str(v["column"]), float(v["max_age_hours"]))
    return ctx


def parse_weekdays(text: str) -> list[int]:
    """``"sat, sun"`` -> ``[5, 6]``; ``"none"`` / ``""`` -> ``[]``."""
    cleaned = text.strip().lower()
    if cleaned in ("", "none", "no", "-"):
        return []
    days = []
    for part in cleaned.replace(";", ",").split(","):
        word = part.strip()[:3]
        if word not in WEEKDAYS:
            raise ContextError(f"not a weekday: {part.strip()!r} (use mon, tue, ... sun)")
        days.append(WEEKDAYS.index(word))
    return sorted(set(days))


def parse_criticality(text: str) -> int:
    cleaned = text.strip().lower()
    by_name = {name: level for level, name in CRITICALITY_LEVELS.items()}
    if cleaned in by_name:
        return by_name[cleaned]
    if cleaned in ("1", "2", "3"):
        return int(cleaned)
    raise ContextError("criticality is 1 (low), 2 (normal) or 3 (critical)")
