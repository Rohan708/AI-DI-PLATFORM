"""In-database queries for relationship discovery: value overlap, orphan counts, and the
query log. All run inside the adapter's read-only, time-limited transactions and
return counts (plus a few row identifiers for orphans)."""

from sqlalchemy import Connection, text

from ai_data_engineer.ingestion.base import InclusionResult, KeyRef, OrphanResult, QueryStat
from ai_data_engineer.ingestion.postgres.profiling import SAMPLE_SEED, quote_ident

_QUERY_LOG_SQL = text("""
SELECT query, calls
FROM pg_stat_statements
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY calls DESC
LIMIT :limit
""")


def read_query_log(conn: Connection, limit: int) -> list[QueryStat]:
    return [
        QueryStat(r.query, int(r.calls)) for r in conn.execute(_QUERY_LOG_SQL, {"limit": limit})
    ]


def value_inclusion(
    conn: Connection, child: KeyRef, parent: KeyRef, max_rows: int, sample_threshold: int
) -> InclusionResult:
    """Share of child rows (with a non-null key) whose key exists in the parent."""
    source, sampled = _table(child, "src", max_rows, sample_threshold)
    child_cols = ", ".join(f"src.{quote_ident(c)}" for c in child.columns)
    sql = (
        f"SELECT count(*) AS checked, count(*) FILTER (WHERE EXISTS ("  # noqa: S608
        f"SELECT 1 FROM {_qualified(parent)} p WHERE {_join(child, parent, 'c')})) AS matched "
        f"FROM (SELECT {child_cols} FROM {source} WHERE {_not_null(child, 'src')} "
        f"LIMIT :limit) c"
    )
    row = conn.execute(text(sql), {"limit": max_rows}).one()
    return InclusionResult(int(row.checked), int(row.matched), sampled)


def count_orphans(
    conn: Connection,
    child: KeyRef,
    parent: KeyRef,
    row_ids: tuple[str, ...],
    sample_size: int,
    sample_threshold: int,
) -> OrphanResult:
    """Child rows whose key has no parent, plus identifiers of up to ``sample_size`` of
    them (the child's primary key if it has one, otherwise the key values)."""
    source, sampled = _table(child, "c", sample_threshold, sample_threshold)
    # Identifiers are quoted by quote_ident; no values are interpolated into the SQL.
    has_parent = (
        f"EXISTS (SELECT 1 FROM {_qualified(parent)} p "  # noqa: S608
        f"WHERE {_join(child, parent, 'c')})"
    )
    counts = conn.execute(
        text(
            f"SELECT count(*) AS checked, "  # noqa: S608
            f"count(*) FILTER (WHERE NOT {has_parent}) AS orphans "
            f"FROM {source} WHERE {_not_null(child, 'c')}"
        )
    ).one()
    id_columns = row_ids or child.columns
    selected = ", ".join(f"c.{quote_ident(col)}::text AS {quote_ident(col)}" for col in id_columns)
    rows = conn.execute(
        text(
            f"SELECT {selected} FROM {source} "  # noqa: S608
            f"WHERE {_not_null(child, 'c')} AND NOT {has_parent} LIMIT :n"
        ),
        {"n": sample_size},
    ).mappings()
    sample = [dict(row) for row in rows]
    return OrphanResult(int(counts.checked), int(counts.orphans), sample, sampled)


def _qualified(key: KeyRef) -> str:
    return f"{quote_ident(key.schema)}.{quote_ident(key.table)}"


def _table(key: KeyRef, alias: str, target_rows: int, sample_threshold: int) -> tuple[str, bool]:
    """FROM-clause for ``key``'s table, sampled when it's estimated above the threshold."""
    rows = key.estimated_rows
    if rows is None or rows <= sample_threshold:
        return f"{_qualified(key)} AS {alias}", False
    percent = min(100.0, target_rows / rows * 100)
    sample = f"TABLESAMPLE SYSTEM ({percent:.6f}) REPEATABLE ({SAMPLE_SEED})"
    return f"{_qualified(key)} AS {alias} {sample}", True


def _not_null(key: KeyRef, alias: str) -> str:
    return " AND ".join(f"{alias}.{quote_ident(c)} IS NOT NULL" for c in key.columns)


def _join(child: KeyRef, parent: KeyRef, alias: str) -> str:
    return " AND ".join(
        f"p.{quote_ident(p)} = {alias}.{quote_ident(c)}"
        for c, p in zip(child.columns, parent.columns, strict=True)
    )
