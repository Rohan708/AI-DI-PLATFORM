"""Counting values that look like personal data, inside the database (Stage 2.6).

One query per table, on at most ``sample_rows`` rows: for every text column, how many
values are non-NULL and how many match each pattern. Patterns are bind parameters; only
the counts come back, never a value. Used by both the Postgres and the generic adapter.

Regular expressions per database: Postgres ``~``, MySQL ``REGEXP_LIKE``, Oracle
``REGEXP_LIKE``, Snowflake ``REGEXP_LIKE`` (which matches the whole value, so the pattern
is wrapped in ``.*``). SQL Server has no regular expressions: not supported there yet.
"""

from typing import Any

from sqlalchemy import Boolean, ColumnElement, Connection, case, func, literal, select

from ai_data_engineer.ingestion.base import KeyRef, PiiCounts
from ai_data_engineer.ingestion.sql.queries import sql_table

REGEX_DIALECTS = frozenset({"postgresql", "mysql", "oracle", "snowflake"})


class RegexNotSupportedError(NotImplementedError):
    pass


def matches(col: Any, pattern: str, dialect: str) -> ColumnElement[bool]:
    """``col`` contains a match of ``pattern`` (case-sensitive)."""
    if dialect == "postgresql":
        return col.op("~", return_type=Boolean)(literal(pattern))  # type: ignore[no-any-return]
    if dialect in ("mysql", "oracle"):
        return func.regexp_like(col, literal(pattern), literal("c"), type_=Boolean)
    if dialect == "snowflake":  # REGEXP_LIKE matches the whole value there
        wrapped = f".*({pattern}).*"
        return func.regexp_like(col, literal(wrapped), literal("cs"), type_=Boolean)
    raise RegexNotSupportedError(f"no regular expressions on {dialect}")


def count_pii(
    conn: Connection,
    table: KeyRef,
    patterns: dict[str, str],
    sample_rows: int,
    dialect: str,
) -> PiiCounts:
    """``table.columns`` are the text columns to look at; ``patterns`` is kind -> regex."""
    t = sql_table(table.schema, table.table, table.columns)
    src = select(*[t.c[c] for c in table.columns]).limit(sample_rows).subquery("s")
    exprs: list[Any] = [func.count().label("rows")]
    for i, name in enumerate(table.columns):
        col = src.c[name]
        exprs.append(func.count(col).label(f"n{i}"))
        for j, pattern in enumerate(patterns.values()):
            hit = matches(col, pattern, dialect)
            exprs.append(func.sum(case((hit, 1), else_=0)).label(f"m{i}_{j}"))
    row = conn.execute(select(*exprs).select_from(src)).mappings().one()
    kinds = list(patterns)
    return PiiCounts(
        rows_checked=int(row["rows"]),
        non_null={name: int(row[f"n{i}"] or 0) for i, name in enumerate(table.columns)},
        matches={
            name: {kind: int(row[f"m{i}_{j}"] or 0) for j, kind in enumerate(kinds)}
            for i, name in enumerate(table.columns)
        },
    )
