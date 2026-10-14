"""Business rules compiled to read-only SQL: one count query (rows judged, rows breaking
the rule) and one query for identifiers of a few offending rows. Identifiers are quoted
with ``quote_ident``; the only values in the SQL are bind parameters."""

from dataclasses import dataclass, field

from sqlalchemy import Connection, text

from lumida.ingestion.base import RuleResult
from lumida.ingestion.postgres.profiling import SAMPLE_SEED, quote_ident
from lumida.rules.spec import (
    AllowedValues,
    CompareColumns,
    CompareConstant,
    NotNull,
    RuleSpec,
    SumMatches,
    Unique,
    Via,
)

# Op values are validated by the rule spec; this map is the only way they reach SQL.
_SQL_OPS = {"<": "<", "<=": "<=", "=": "=", ">=": ">=", ">": ">", "<>": "<>"}


@dataclass
class _Query:
    source: str  # FROM clause
    judged: str  # rows the rule can judge
    broken: str  # rows breaking it
    params: dict[str, object] = field(default_factory=dict)


def check_rule(
    conn: Connection,
    spec: RuleSpec,
    row_ids: tuple[str, ...],
    estimated_rows: int | None,
    sample_size: int,
    sample_threshold: int,
) -> RuleResult:
    sampled = estimated_rows is not None and estimated_rows > sample_threshold
    base = _qualified(spec.table) + " AS t"
    if sampled and estimated_rows:
        percent = min(100.0, sample_threshold / estimated_rows * 100)
        base += f" TABLESAMPLE SYSTEM ({percent:.6f}) REPEATABLE ({SAMPLE_SEED})"
    q = compile_rule(spec, base)
    counts = conn.execute(
        text(
            f"SELECT count(*) FILTER (WHERE {q.judged}) AS checked, "  # noqa: S608
            f"count(*) FILTER (WHERE {q.broken}) AS broken FROM {q.source}"
        ),
        q.params,
    ).one()
    sample: list[dict[str, str]] = []
    if row_ids and counts.broken:
        selected = ", ".join(f"t.{quote_ident(c)}::text AS {quote_ident(c)}" for c in row_ids)
        rows = conn.execute(
            text(f"SELECT {selected} FROM {q.source} WHERE {q.broken} LIMIT :n"),  # noqa: S608
            {**q.params, "n": sample_size},
        ).mappings()
        sample = [dict(row) for row in rows]
    return RuleResult(int(counts.checked), int(counts.broken), sample, sampled)


def compile_rule(spec: RuleSpec, base: str) -> _Query:
    """The FROM clause and conditions for ``spec``; ``base`` is ``<table> AS t``."""
    col = f"t.{quote_ident(spec.column)}"
    match spec:
        case CompareColumns(via=None):
            other = f"t.{quote_ident(spec.other_column)}"
            return _comparison(base, col, _SQL_OPS[spec.op], other)
        case CompareColumns(via=Via() as via):
            join = " AND ".join(
                f"p.{quote_ident(parent)} = t.{quote_ident(child)}" for child, parent in via.on
            )
            source = f"{base} JOIN {_qualified(via.parent_table)} AS p ON {join}"
            other = f"p.{quote_ident(spec.other_column)}"
            return _comparison(source, col, _SQL_OPS[spec.op], other)
        case CompareConstant():
            judged = f"{col} IS NOT NULL"
            value = "CAST(:value AS double precision)"
            broken = f"{judged} AND NOT ({col} {_SQL_OPS[spec.op]} {value})"
            return _Query(base, judged, broken, {"value": spec.value})
        case SumMatches():
            match_ = " AND ".join(
                f"c.{quote_ident(child)} = t.{quote_ident(parent)}" for child, parent in spec.on
            )
            lines = (
                f"SELECT sum(c.{quote_ident(spec.child_column)}) AS total, count(*) AS n "  # noqa: S608
                f"FROM {_qualified(spec.child_table)} AS c WHERE {match_}"
            )
            source = f"{base} CROSS JOIN LATERAL ({lines}) AS s"
            judged = f"{col} IS NOT NULL AND s.n > 0"  # a parent without children isn't judged
            broken = f"{judged} AND abs({col} - s.total) > CAST(:tolerance AS numeric)"
            return _Query(source, judged, broken, {"tolerance": spec.tolerance})
        case NotNull():
            return _Query(base, "true", f"{col} IS NULL")
        case Unique():
            # Rows whose value another row also has (counted over the whole table).
            c = quote_ident(spec.column)
            shared = (
                f"SELECT u.{c} FROM {_qualified(spec.table)} AS u "  # noqa: S608
                f"WHERE u.{c} IS NOT NULL GROUP BY u.{c} HAVING count(*) > 1"
            )
            judged = f"{col} IS NOT NULL"
            return _Query(base, judged, f"{judged} AND {col} IN ({shared})")
        case AllowedValues():
            judged = f"{col} IS NOT NULL"
            broken = f"{judged} AND NOT ({col}::text = ANY(CAST(:allowed AS text[])))"
            return _Query(base, judged, broken, {"allowed": list(spec.values)})
    raise AssertionError(spec)  # pragma: no cover


def _comparison(source: str, left: str, op: str, right: str) -> _Query:
    judged = f"{left} IS NOT NULL AND {right} IS NOT NULL"
    return _Query(source, judged, f"{judged} AND NOT ({left} {op} {right})")


def _qualified(ref: str) -> str:
    schema, _, table = ref.partition(".")
    return f"{quote_ident(schema)}.{quote_ident(table)}"
