"""Questions the engine asks about a source, and what each answer turns into.

The engine looks at what it measured and asks only where an answer changes detection,
each question pre-filled with its best guess:

| Question | From | A "yes" (or a value) becomes |
|---|---|---|
| Closed on some weekdays? | once per source | no volume / staleness alarms for closed days |
| Does ``A.x`` point to ``B.y``? | a link discovery isn't sure of | confirmed (or rejected) |
| Should ``x`` always be unique? | unique now, not enforced | a ``unique`` rule |
| Must ``x`` always be filled? | no NULLs now, column allows them | a ``not_null`` rule |
| Are these all the allowed values? | few distinct values | an ``allowed_values`` rule |
| How late is too late? | a table with a time column | a freshness deadline |
| How important is this table? | the largest tables first | health and alert weighting |

Answered questions aren't asked again; "don't know" (``?`` or blank) comes back after
``REASK_AFTER_DAYS``. Expectations become rules (origin ``user``, active), so they run
as plain SQL every night and are reviewed like every other rule.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lumida.context.store import (
    CRITICALITY_LEVELS,
    WEEKDAYS,
    load_context,
    parse_criticality,
    parse_weekdays,
    set_note,
)
from lumida.discovery.catalog import Catalog, ColumnInfo, TableInfo, load_catalog
from lumida.discovery.naming import KEY_WORDS, MEASURE_WORDS, tokens
from lumida.discovery.settings import DEFAULT_DISCOVERY_SETTINGS
from lumida.discovery.store import current_relationships, review
from lumida.graph.models import (
    DataSource,
    Origin,
    QuestionAnswer,
    Relationship,
    RelationshipKind,
    RelationshipStatus,
    RuleStatus,
    TypeFamily,
)
from lumida.rules.spec import (
    AllowedValues,
    NotNull,
    RuleSpec,
    Unique,
    describe,
    signature,
)
from lumida.rules.store import known_links, rule_with_signature, save_rule

REASK_AFTER_DAYS = 30
MIN_ROWS = 20  # tables smaller than this give too little evidence to ask about
MAX_ALLOWED_VALUES = 20  # longer lists aren't "a list of allowed values"
TIME_FAMILIES = (TypeFamily.TIMESTAMP, TypeFamily.DATE)
# Column names that usually record when a row was loaded or last changed (best clocks).
CLOCK_WORDS = ("loaded", "refreshed", "updated", "modified", "created", "inserted")
YES, NO = frozenset({"y", "yes", "true", "1"}), frozenset({"n", "no", "false", "0"})


class AnswerError(ValueError):
    pass


@dataclass(frozen=True)
class Question:
    key: str  # stable identity, e.g. "unique:<column key>"
    kind: str  # closed_weekdays | link | unique | required | allowed | freshness | ...
    prompt: str
    hint: str  # what to type
    default: str  # the engine's best guess (Enter accepts it)
    data: dict[str, Any] = field(default_factory=dict)


def generate(
    session: Session, source: DataSource, *, now: datetime, limit: int = 10
) -> list[Question]:
    """The most useful unanswered questions about ``source``, best first."""
    catalog = load_catalog(session, source)
    context = load_context(session, source)
    answered = _answered(session, source, now)
    candidates: list[Question] = []
    if "closed_weekdays" not in answered and not context.closed_weekdays:
        candidates.append(
            Question(
                "closed_weekdays", "closed_weekdays",
                "Is the business closed on some weekdays (no orders, no loads)?",
                "e.g. sat,sun, or none", "none",
            )
        )  # fmt: skip
    candidates += _link_questions(session, catalog)
    tables = [t for t in catalog.tables.values() if t.kind == "table"]
    keyed = _key_columns(catalog, known_links(session, catalog))
    for maker in (_unique_questions, _required_questions, _allowed_questions):
        for table in sorted(tables, key=lambda t: -(t.estimated_rows or 0)):
            candidates += maker(table, keyed.get(table.ref, set()))
    candidates += _freshness_questions(tables, context.freshness)
    for table in sorted(tables, key=lambda t: -(t.estimated_rows or 0))[:5]:
        if table.asset_key not in context.criticality:
            candidates.append(
                Question(
                    f"criticality:{table.asset_key}", "criticality",
                    f"How important is {table.ref}?",
                    "1 = low, 2 = normal, 3 = critical", "2",
                    {"asset_key": str(table.asset_key)},
                )
            )  # fmt: skip
    return [q for q in candidates if q.key not in answered][:limit]


def answer(
    session: Session,
    source: DataSource,
    question: Question,
    text: str,
    *,
    by: str,
    now: datetime,
) -> str:
    """Apply an answer; returns what it changed, in one line."""
    reply = text.strip()
    if reply in ("", "?", "skip", "don't know", "dont know"):
        _remember(session, source, question, {"answer": "skip"}, by, now)
        return "skipped (asked again later)"
    outcome = APPLY[question.kind](session, source, question, reply, by, now)
    _remember(session, source, question, {"answer": reply}, by, now)
    return outcome


def ask_all(
    session: Session,
    source: DataSource,
    questions: list[Question],
    read: Callable[[Question], str],
    *,
    by: str,
    now: datetime,
) -> list[tuple[Question, str]]:
    """Ask each question with ``read`` (``input`` in the CLI; a script in tests)."""
    out = []
    for q in questions:
        reply = read(q)
        try:
            out.append((q, answer(session, source, q, reply or q.default, by=by, now=now)))
        except AnswerError as exc:
            out.append((q, f"not understood ({exc}); asked again next time"))
    return out


# --- generating ----------------------------------------------------------------------------


def _answered(session: Session, source: DataSource, now: datetime) -> set[str]:
    rows = session.scalars(select(QuestionAnswer).where(QuestionAnswer.data_source_id == source.id))
    fresh_skip = now - timedelta(days=REASK_AFTER_DAYS)
    return {
        r.question_key
        for r in rows
        if r.answer.get("answer") != "skip" or r.answered_at >= fresh_skip
    }


def _link_questions(session: Session, catalog: Catalog) -> list[Question]:
    threshold = DEFAULT_DISCOVERY_SETTINGS.orphan_check_min_confidence
    names = {
        c.column_key: (t.ref, c.name) for t in catalog.tables.values() for c in t.columns.values()
    }
    out = []
    for rel in current_relationships(session, catalog):
        undecided = rel.status is RelationshipStatus.PROPOSED
        if rel.kind is not RelationshipKind.INFERRED or not undecided:
            continue
        if rel.confidence is not None and rel.confidence >= threshold:
            continue
        text = _link_text(rel, names)
        if text is None:
            continue
        ai = rel.evidence.get("ai_review", {})
        guess = "yes" if ai.get("verdict") != "unlikely" else "no"
        out.append(
            Question(
                f"link:{rel.signature}", "link",
                f"Does {text}? (discovery is {rel.confidence or 0:.0%} sure"
                + (f"; AI says {ai['verdict']}" if ai.get("verdict") else "") + ")",
                "yes / no", guess, {"relationship_id": str(rel.id)},
            )
        )  # fmt: skip
    return out


def _link_text(rel: Relationship, names: dict[Any, tuple[str, str]]) -> str | None:
    child_cols, parent_cols = [], []
    child_table = parent_table = None
    for pair in rel.columns:
        child, parent = names.get(pair.from_column_key), names.get(pair.to_column_key)
        if child is None or parent is None:
            return None  # a column was dropped since
        child_table, parent_table = child[0], parent[0]
        child_cols.append(child[1])
        parent_cols.append(parent[1])
    if child_table is None or parent_table is None:
        return None
    return (
        f"{child_table}({', '.join(child_cols)}) point to {parent_table}({', '.join(parent_cols)})"
    )


def _key_columns(catalog: Catalog, links: list[Any]) -> dict[str, set[str]]:
    """Columns already covered by keys or relationships (no question needed)."""
    out: dict[str, set[str]] = {}
    for table in catalog.tables.values():
        cols = set(table.primary_key) | {c for key in table.unique_keys for c in key}
        out[table.ref] = cols
    for child, child_cols, parent, parent_cols in links:
        out.setdefault(child, set()).update(child_cols)
        out.setdefault(parent, set()).update(parent_cols)
    return out


def _measured(table: TableInfo, column: ColumnInfo) -> bool:
    return (column.row_count or 0) >= MIN_ROWS and (table.estimated_rows or 0) >= MIN_ROWS


def _unique_questions(table: TableInfo, keyed: set[str]) -> list[Question]:
    out = []
    for c in table.columns.values():
        if c.name in keyed or c.family not in (TypeFamily.STRING, TypeFamily.INTEGER):
            continue
        words = set(tokens(c.name))
        if not (_measured(table, c) and c.effectively_unique) or words & MEASURE_WORDS:
            continue
        if c.family is TypeFamily.INTEGER and not words & KEY_WORDS:
            continue  # unique counts or amounts are coincidence; ids/numbers/codes aren't
        out.append(
            Question(
                f"unique:{c.column_key}", "unique",
                f"Every {table.ref}.{c.name} value is different today. "
                "Should it always be unique?",
                "yes / no", "yes", {"table": table.ref, "column": c.name},
            )
        )  # fmt: skip
    return out


def _required_questions(table: TableInfo, keyed: set[str]) -> list[Question]:
    out = []
    for c in table.columns.values():
        if c.name in keyed or not c.is_nullable or not _measured(table, c):
            continue
        if c.null_count != 0:
            continue
        out.append(
            Question(
                f"required:{c.column_key}", "required",
                f"{table.ref}.{c.name} is never empty today, but the database allows it. "
                "Must it always be filled?",
                "yes / no", "yes", {"table": table.ref, "column": c.name},
            )
        )  # fmt: skip
    return out


def _allowed_questions(table: TableInfo, keyed: set[str]) -> list[Question]:
    out = []
    for c in table.columns.values():
        values = c.top_values
        if c.name in keyed or not values or not _measured(table, c):
            continue
        if c.family not in (TypeFamily.STRING, TypeFamily.BOOLEAN):
            continue
        if not 2 <= len(values) <= MAX_ALLOWED_VALUES:
            continue
        listed = ", ".join(sorted(values))
        out.append(
            Question(
                f"allowed:{c.column_key}", "allowed",
                f"{table.ref}.{c.name} has these values today: {listed}. Is that the full list "
                "of allowed values? (yes, no, or type the full list)",
                "yes / no / a,b,c", "yes",
                {"table": table.ref, "column": c.name, "values": sorted(values)},
            )
        )  # fmt: skip
    return out


def _freshness_questions(
    tables: list[TableInfo], deadlines: dict[Any, tuple[str, float]]
) -> list[Question]:
    out = []
    for table in tables:
        if table.asset_key in deadlines or (table.estimated_rows or 0) < MIN_ROWS:
            continue
        clock = _clock(table)
        if clock is None:
            continue
        out.append(
            Question(
                f"freshness:{table.asset_key}", "freshness",
                f"How many hours may {table.ref} go without new rows (by {clock}) before "
                "it's late? (blank = no limit)",
                "hours, e.g. 26", "",
                {"asset_key": str(table.asset_key), "column": clock, "table": table.ref},
            )
        )  # fmt: skip
    # Tables named like loads (loaded_at, refreshed_at) first: deadlines matter most there.
    out.sort(key=lambda q: 0 if q.data["column"].startswith(CLOCK_WORDS[:2]) else 1)
    return out[:5]


def _clock(table: TableInfo) -> str | None:
    times = [c for c in table.columns.values() if c.family in TIME_FAMILIES]
    for word in CLOCK_WORDS:
        for c in times:
            if word in c.name.lower():
                return c.name
    return None


# --- applying ------------------------------------------------------------------------------

Apply = Callable[[Session, DataSource, Question, str, str, datetime], str]


def _yes(reply: str) -> bool:
    lowered = reply.lower()
    if lowered in YES:
        return True
    if lowered in NO:
        return False
    raise AnswerError("answer yes or no")


def _closed(s: Session, src: DataSource, q: Question, reply: str, by: str, now: datetime) -> str:
    days = parse_weekdays(reply)
    set_note(s, src, "closed_weekdays", {"days": days}, by=by, now=now)
    return f"closed on {', '.join(WEEKDAYS[d] for d in days)}" if days else "open every day"


def _link(s: Session, src: DataSource, q: Question, reply: str, by: str, now: datetime) -> str:
    rel = s.get_one(Relationship, q.data["relationship_id"])
    status = RelationshipStatus.CONFIRMED if _yes(reply) else RelationshipStatus.REJECTED
    review(s, rel, status, by, now)
    return f"relationship {status.value}"


def _rule_for(kind: str) -> Apply:
    def apply(s: Session, src: DataSource, q: Question, reply: str, by: str, now: datetime) -> str:
        table, column = q.data["table"], q.data["column"]
        if kind == "allowed":
            lowered = reply.lower()
            if lowered in NO:
                return "noted: values aren't a fixed list"
            values = q.data["values"] if lowered in YES else _listed(reply)
            spec: RuleSpec = AllowedValues(table=table, column=column, values=values)
        elif not _yes(reply):
            return "noted: not an expectation"
        elif kind == "unique":
            spec = Unique(table=table, column=column)
        else:
            spec = NotNull(table=table, column=column)
        return _add_rule(s, src, spec, by, now)

    return apply


def _listed(reply: str) -> list[str]:
    values = [v.strip() for v in reply.split(",") if v.strip()]
    if not values:
        raise AnswerError("type yes, no, or the allowed values separated by commas")
    return values


def _add_rule(s: Session, src: DataSource, spec: RuleSpec, by: str, now: datetime) -> str:
    if rule_with_signature(s, src, signature(spec)) is not None:
        return f"already a rule: {describe(spec)}"
    save_rule(
        s, source=src, catalog=load_catalog(s, src), spec=spec, origin=Origin.USER,
        status=RuleStatus.ACTIVE, created_by=by, now=now,
    )  # fmt: skip
    return f"rule added: {describe(spec)}"


def _freshness(s: Session, src: DataSource, q: Question, reply: str, by: str, now: datetime) -> str:
    try:
        hours = float(reply)
    except ValueError as exc:
        raise AnswerError("type a number of hours, or leave it blank") from exc
    if hours <= 0:
        raise AnswerError("hours must be more than 0")
    set_note(
        s, src, "freshness", {"column": q.data["column"], "max_age_hours": hours},
        by=by, now=now, asset_key=uuid.UUID(q.data["asset_key"]),
    )  # fmt: skip
    return f"deadline: {q.data['table']} late after {hours:g} h without new rows"


def _criticality(
    s: Session, src: DataSource, q: Question, reply: str, by: str, now: datetime
) -> str:
    level = parse_criticality(reply)
    set_note(
        s, src, "criticality", {"level": level}, by=by, now=now,
        asset_key=uuid.UUID(q.data["asset_key"]),
    )  # fmt: skip
    return f"importance: {CRITICALITY_LEVELS[level]}"


APPLY: dict[str, Apply] = {
    "closed_weekdays": _closed,
    "link": _link,
    "unique": _rule_for("unique"),
    "required": _rule_for("required"),
    "allowed": _rule_for("allowed"),
    "freshness": _freshness,
    "criticality": _criticality,
}


def _remember(
    s: Session, src: DataSource, q: Question, value: dict[str, Any], by: str, now: datetime
) -> None:
    row = s.scalar(
        select(QuestionAnswer).where(
            QuestionAnswer.data_source_id == src.id, QuestionAnswer.question_key == q.key
        )
    )
    if row is None:
        row = QuestionAnswer(tenant_id=src.tenant_id, data_source_id=src.id, question_key=q.key)
        s.add(row)
    row.answer, row.answered_by, row.answered_at = value, by, now
    s.flush()
