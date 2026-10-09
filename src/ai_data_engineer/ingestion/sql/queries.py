"""Measurements and checks for any SQL database, built with SQLAlchemy Core so each
dialect writes its own SQL. The same contract as the Postgres adapter: everything is
counted inside the database; only aggregates and a few row identifiers come back.

Sampling: large tables are read through ``SELECT ... LIMIT n`` (``TOP``/``FETCH FIRST``
per dialect), which every database supports. Unlike Postgres' ``TABLESAMPLE`` these are
the *first* n rows the database returns, not a random sample, so findings on sampled
tables say so and their numbers are estimates.
"""

import math
import operator
from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Connection,
    FromClause,
    Select,
    and_,
    case,
    column,
    distinct,
    exists,
    func,
    literal,
    not_,
    select,
    table,
)

from ai_data_engineer.graph.models import TypeFamily
from ai_data_engineer.ingestion.base import (
    ColumnMeasurement,
    DiscoveredTable,
    InclusionResult,
    KeyRef,
    OrphanResult,
    OutlierResult,
    RuleResult,
)
from ai_data_engineer.ingestion.settings import ScanSettings
from ai_data_engineer.ingestion.sql.dialects import DialectProfile
from ai_data_engineer.rules.spec import (
    CompareColumns,
    CompareConstant,
    RuleSpec,
    SumMatches,
    Via,
)

MAD_TO_SIGMA = 1.4826
_ORDERABLE = {
    TypeFamily.INTEGER, TypeFamily.DECIMAL, TypeFamily.FLOAT, TypeFamily.DATE,
    TypeFamily.TIMESTAMP, TypeFamily.TIME, TypeFamily.STRING,
}  # fmt: skip
_NUMERIC = {TypeFamily.INTEGER, TypeFamily.DECIMAL, TypeFamily.FLOAT}
_NO_DISTINCT = {TypeFamily.JSON, TypeFamily.ARRAY, TypeFamily.BINARY, TypeFamily.OTHER}
_VALUE_FAMILIES = {TypeFamily.STRING}
_TOP_VALUE_FAMILIES = {TypeFamily.STRING, TypeFamily.BOOLEAN}
_OPS: dict[str, Callable[[Any, Any], ColumnElement[bool]]] = {
    "<": operator.lt, "<=": operator.le, "=": operator.eq, ">=": operator.ge,
    ">": operator.gt, "<>": operator.ne,
}  # fmt: skip


def sql_table(schema: str, name: str, columns: tuple[str, ...] | list[str]) -> Any:
    return table(name, *[column(c) for c in dict.fromkeys(columns)], schema=schema)


def ref_table(ref: str, columns: tuple[str, ...] | list[str]) -> Any:
    """A table given as ``schema.table`` (the form rules use)."""
    schema, name = ref.split(".", 1)
    return sql_table(schema, name, columns)


def as_text(value: Any) -> str | None:
    """A value as text, close to how Postgres prints it (so history compares across scans)."""
    if value is None or isinstance(value, bytes | bytearray | memoryview):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _number(value: Any) -> float | None:
    return float(value) if value is not None else None


def _limited(query: Select[Any], rows: int | None, limit: int, threshold: int) -> tuple[Any, bool]:
    """``query`` as a subquery, limited to ``limit`` rows if the table is large."""
    if rows is not None and rows > threshold:
        return query.limit(limit).subquery("s"), True
    return query.subquery("s"), False


# --- profiling ----------------------------------------------------------------------------


def run_profile(
    conn: Connection, table_: DiscoveredTable, settings: ScanSettings, d: DialectProfile
) -> tuple[float, int, list[ColumnMeasurement]]:
    """(sample fraction, rows measured, per-column measurements)."""
    obs = table_.observation
    names = [c.name for c in obs.columns]
    base = sql_table(obs.schema_name, obs.name, names)
    rows = table_.estimated_rows
    threshold = settings.sample_row_threshold
    src, sampled = _limited(select(*[base.c[n] for n in names]), rows, threshold, threshold)
    fraction = threshold / rows if sampled and rows else 1.0
    exprs: list[Any] = [func.count().label("row_count")]
    for i, c in enumerate(obs.columns):
        col, family = src.c[c.name], c.type_family
        exprs.append(func.count(col).label(f"c{i}_nonnull"))
        if family not in _NO_DISTINCT:
            exprs.append(func.count(distinct(col)).label(f"c{i}_distinct"))
        may_show = family not in _VALUE_FAMILIES or settings.allow_value_samples
        if family in _ORDERABLE and may_show:
            exprs += [func.min(col).label(f"c{i}_min"), func.max(col).label(f"c{i}_max")]
        if family in _NUMERIC:
            exprs.append(func.avg(col * 1.0).label(f"c{i}_mean"))
            exprs.append(getattr(func, d.stddev)(col * 1.0).label(f"c{i}_stddev"))
        if family is TypeFamily.STRING:
            length = getattr(func, d.length)(col) * 1.0
            exprs.append(func.avg(length).label(f"c{i}_avg_length"))
            exprs.append(func.count(distinct(func.lower(col))).label(f"c{i}_distinct_ci"))
    row = conn.execute(select(*exprs).select_from(src)).mappings().one()
    measured = int(row["row_count"])
    out = []
    for i, c in enumerate(obs.columns):
        distinct_n = row.get(f"c{i}_distinct")
        top = None
        if (
            settings.allow_value_samples
            and c.type_family in _TOP_VALUE_FAMILIES
            and distinct_n is not None
            and 0 < distinct_n <= settings.top_values_max_distinct
        ):
            top = _top_values(conn, src.c[c.name], src, settings.top_values_max_distinct)
        ci = row.get(f"c{i}_distinct_ci")
        out.append(
            ColumnMeasurement(
                name=c.name,
                row_count=measured,
                null_count=measured - int(row[f"c{i}_nonnull"]),
                distinct_count=int(distinct_n) if distinct_n is not None else None,
                distinct_is_approx=sampled,
                min_repr=as_text(row.get(f"c{i}_min")),
                max_repr=as_text(row.get(f"c{i}_max")),
                mean=_number(row.get(f"c{i}_mean")),
                stddev=_number(row.get(f"c{i}_stddev")),
                avg_length=_number(row.get(f"c{i}_avg_length")),
                top_values=top,
                distinct_case_insensitive=int(ci) if ci is not None else None,
            )
        )
    return fraction, measured, out


def _top_values(conn: Connection, col: Any, src: FromClause, limit: int) -> list[dict[str, Any]]:
    n = func.count()
    query = (
        select(col.label("value"), n.label("n"))
        .select_from(src)
        .where(col.is_not(None))
        .group_by(col)
        .order_by(n.desc(), col)
        .limit(limit)
    )
    return [{"value": as_text(r.value), "count": int(r.n)} for r in conn.execute(query)]


# --- relationships ------------------------------------------------------------------------


def _key_rows(
    key: KeyRef, extra: tuple[str, ...], max_rows: int, threshold: int
) -> tuple[Any, bool]:
    t = sql_table(key.schema, key.table, (*key.columns, *extra))
    not_null = and_(*[t.c[c].is_not(None) for c in key.columns])
    wanted = list(dict.fromkeys((*key.columns, *extra)))
    query = select(*[t.c[c] for c in wanted]).where(not_null)
    return _limited(query, key.estimated_rows, max_rows, threshold)


def _has_parent(src: Any, child: KeyRef, parent: KeyRef) -> ColumnElement[bool]:
    p = sql_table(parent.schema, parent.table, parent.columns).alias("p")
    match = [p.c[pc] == src.c[cc] for cc, pc in zip(child.columns, parent.columns, strict=True)]
    return exists().where(and_(*match))


def value_inclusion(
    conn: Connection, child: KeyRef, parent: KeyRef, max_rows: int, threshold: int
) -> InclusionResult:
    src, sampled = _key_rows(child, (), max_rows, threshold)
    found = _has_parent(src, child, parent)
    row = conn.execute(
        select(func.count().label("checked"), func.sum(case((found, 1), else_=0)).label("hit"))
        .select_from(src)
    ).one()
    return InclusionResult(int(row.checked or 0), int(row.hit or 0), sampled)


def count_orphans(
    conn: Connection,
    child: KeyRef,
    parent: KeyRef,
    row_ids: tuple[str, ...],
    sample_size: int,
    threshold: int,
) -> OrphanResult:
    ids = row_ids or child.columns
    src, sampled = _key_rows(child, ids, threshold, threshold)
    orphan = not_(_has_parent(src, child, parent))
    row = conn.execute(
        select(func.count().label("checked"), func.sum(case((orphan, 1), else_=0)).label("bad"))
        .select_from(src)
    ).one()
    bad = int(row.bad or 0)
    sample = _ids(conn, src, ids, orphan, sample_size) if bad else []
    return OrphanResult(int(row.checked or 0), bad, sample, sampled)


def _ids(
    conn: Connection, src: Any, ids: tuple[str, ...], where: ColumnElement[bool], n: int
) -> list[dict[str, str]]:
    rows = conn.execute(select(*[src.c[c] for c in ids]).select_from(src).where(where).limit(n))
    return [{c: as_text(v) or "" for c, v in zip(ids, r, strict=True)} for r in rows]


# --- business rules -----------------------------------------------------------------------

_LEFT, _RIGHT, _TOTAL, _COUNT = "aide_left", "aide_right", "aide_total", "aide_children"


def rule_rows(spec: RuleSpec, row_ids: tuple[str, ...]) -> tuple[Select[Any], tuple[str, ...]]:
    """One row per row of ``spec.table`` with the values the rule compares, labelled
    ``aide_*``, plus the identifier columns (labelled ``aide_id_<n>``)."""
    match spec:
        case CompareColumns(via=None):
            t = ref_table(spec.table, (spec.column, spec.other_column, *row_ids))
            values = [t.c[spec.column].label(_LEFT), t.c[spec.other_column].label(_RIGHT)]
            source: Any = t
        case CompareColumns(via=Via() as via):
            t = ref_table(spec.table, (spec.column, *[a for a, _ in via.on], *row_ids))
            p = ref_table(via.parent_table, (spec.other_column, *[b for _, b in via.on]))
            p = p.alias("p")
            source = t.join(p, and_(*[p.c[b] == t.c[a] for a, b in via.on]))
            values = [t.c[spec.column].label(_LEFT), p.c[spec.other_column].label(_RIGHT)]
        case CompareConstant():
            t = ref_table(spec.table, (spec.column, *row_ids))
            values, source = [t.c[spec.column].label(_LEFT)], t
        case SumMatches():
            t = ref_table(spec.table, (spec.column, *[b for _, b in spec.on], *row_ids))
            c = ref_table(spec.child_table, (spec.child_column, *[a for a, _ in spec.on]))
            c = c.alias("c")
            linked = and_(*[c.c[a] == t.c[b] for a, b in spec.on])
            total = select(func.sum(c.c[spec.child_column])).where(linked).scalar_subquery()
            children = select(func.count()).select_from(c).where(linked).scalar_subquery()
            values = [t.c[spec.column].label(_LEFT), total.label(_TOTAL), children.label(_COUNT)]
            source = t
    labels = tuple(f"aide_id_{i}" for i in range(len(row_ids)))
    ids = [t.c[r].label(label) for r, label in zip(row_ids, labels, strict=True)]
    return select(*values, *ids).select_from(source), labels


def rule_conditions(spec: RuleSpec, s: Any) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    """(judged, broken) over the rows of ``rule_rows``."""
    left = s.c[_LEFT]
    match spec:
        case CompareColumns():
            right = s.c[_RIGHT]
            judged = and_(left.is_not(None), right.is_not(None))
            return judged, and_(judged, not_(_OPS[spec.op](left, right)))
        case CompareConstant():
            judged = left.is_not(None)
            return judged, and_(judged, not_(_OPS[spec.op](left, literal(spec.value))))
        case SumMatches():
            judged = and_(left.is_not(None), s.c[_COUNT] > 0)
            gap = func.abs(left - s.c[_TOTAL])
            return judged, and_(judged, gap > literal(spec.tolerance))
    raise AssertionError(spec)  # pragma: no cover


def check_rule(
    conn: Connection,
    spec: RuleSpec,
    row_ids: tuple[str, ...],
    estimated_rows: int | None,
    sample_size: int,
    threshold: int,
) -> RuleResult:
    rows, labels = rule_rows(spec, row_ids)
    s, sampled = _limited(rows, estimated_rows, threshold, threshold)
    judged, broken = rule_conditions(spec, s)
    row = conn.execute(
        select(
            func.sum(case((judged, 1), else_=0)).label("checked"),
            func.sum(case((broken, 1), else_=0)).label("bad"),
        ).select_from(s)
    ).one()
    bad = int(row.bad or 0)
    sample: list[dict[str, str]] = []
    if bad and labels:
        found = conn.execute(select(*[s.c[x] for x in labels]).where(broken).limit(sample_size))
        sample = [{r: as_text(v) or "" for r, v in zip(row_ids, f, strict=True)} for f in found]
    return RuleResult(int(row.checked or 0), bad, sample, sampled)


# --- row-level outliers -------------------------------------------------------------------


def row_outliers(
    conn: Connection,
    key: KeyRef,
    row_ids: tuple[str, ...],
    d: DialectProfile,
    *,
    sigmas: float,
    min_ratio: float,
    min_spread: float,
    sample_size: int,
    threshold: int,
) -> OutlierResult:
    """Like the Postgres version, but medians come from ORDER BY ... OFFSET (portable)
    instead of percentile_cont, which not every database has."""
    name = key.columns[0]
    t = sql_table(key.schema, key.table, (name, *row_ids))
    labels = tuple(f"aide_id_{i}" for i in range(len(row_ids)))
    ids = [t.c[r].label(x) for r, x in zip(row_ids, labels, strict=True)]
    inner = select(t.c[name].label("v"), *ids).where(t.c[name] > 0)
    s, sampled = _limited(inner, key.estimated_rows, threshold, threshold)
    v = s.c.v
    n = int(conn.execute(select(func.count()).select_from(s)).scalar() or 0)
    if n == 0:
        return OutlierResult(0, None, None, 0, None, [], sampled)
    middle = (n - 1) // 2
    median = float(conn.execute(select(v).order_by(v).offset(middle).limit(1)).scalar_one())
    centre = math.log(median)
    deviation = func.abs(getattr(func, d.ln)(v) - literal(centre))
    mad_query = select(deviation).order_by(deviation).offset(middle).limit(1)
    mad = float(conn.execute(mad_query).scalar_one())
    spread = max(MAD_TO_SIGMA * mad, min_spread)
    cutoff = math.exp(centre + max(sigmas * spread, math.log(min_ratio)))
    above = v > literal(cutoff)
    stats = conn.execute(
        select(func.sum(case((above, 1), else_=0)).label("bad"), func.max(v).label("top"))
        .select_from(s)
    ).one()
    bad = int(stats.bad or 0)
    sample: list[dict[str, str]] = []
    if bad and labels:
        found = conn.execute(
            select(*[s.c[x] for x in labels]).where(above).order_by(v.desc()).limit(sample_size)
        )
        sample = [
            {r: as_text(val) or "" for r, val in zip(row_ids, f, strict=True)} for f in found
        ]
    top = float(stats.top) if stats.top is not None else None
    return OutlierResult(
        checked_rows=n, median=median, cutoff=cutoff, outlier_rows=bad,
        max_ratio=top / median if top is not None else None, sample=sample, sampled=sampled,
    )  # fmt: skip

