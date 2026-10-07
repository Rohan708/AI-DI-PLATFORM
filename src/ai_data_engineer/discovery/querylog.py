"""Find the joins an application actually runs, from its query log.

``pg_stat_statements`` (and equivalents in other databases) keeps normalised statements
with call counts. We parse each with sqlglot and collect equality conditions between
columns of two different tables, e.g. ``JOIN customers c ON o.cust_no = c.id`` gives
``shop.orders.cust_no = shop.customers.id`` with the statement's call count.
"""

import logging
import re
from collections import Counter
from collections.abc import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from ai_data_engineer.ingestion.base import QueryStat

ColumnRef = tuple[str, str, str]  # (schema, table, column); schema may be "" if unknown

# Only statements that can contain a join condition are parsed (SHOW, SET, CREATE ... are
# skipped), and sqlglot's "falling back to Command" warnings are kept out of the output.
_MAY_JOIN = re.compile(
    r"^\s*(SELECT|WITH|INSERT|UPDATE|DELETE)\b.*=", re.IGNORECASE | re.DOTALL
)
logging.getLogger("sqlglot").setLevel(logging.ERROR)


def join_pairs(sql: str, *, dialect: str = "postgres") -> list[tuple[ColumnRef, ColumnRef]]:
    """Column pairs compared with ``=`` across two different tables. Unparseable
    statements and unqualified columns are ignored (no guessing)."""
    if not _MAY_JOIN.match(sql):
        return []
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except SqlglotError:
        return []
    if tree is None:
        return []

    tables: dict[str, tuple[str, str]] = {}
    for table in tree.find_all(exp.Table):
        ref = (_ident(table.args.get("db")), _ident(table.this))
        tables[_alias_key(table.alias_or_name)] = ref
        tables.setdefault(_alias_key(table.name), ref)

    pairs = []
    for eq in tree.find_all(exp.EQ):
        left, right = eq.left, eq.right
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            continue
        a, b = _resolve(left, tables), _resolve(right, tables)
        if a and b and a[:2] != b[:2]:
            pairs.append((a, b))
    return pairs


def join_counts(stats: Iterable[QueryStat]) -> Counter[frozenset[ColumnRef]]:
    """Total calls per joined column pair (order-insensitive)."""
    counts: Counter[frozenset[ColumnRef]] = Counter()
    for stat in stats:
        for a, b in join_pairs(stat.query):
            counts[frozenset((a, b))] += stat.calls
    return counts


def _resolve(column: exp.Column, tables: dict[str, tuple[str, str]]) -> ColumnRef | None:
    qualifier = column.table
    if not qualifier:
        return None
    table = tables.get(_alias_key(qualifier))
    if table is None:
        return None
    return (table[0], table[1], _ident(column.this))


def _ident(node: exp.Expression | None) -> str:
    """Postgres folds unquoted identifiers to lowercase; quoted ones keep their case."""
    if node is None:
        return ""
    if isinstance(node, exp.Identifier):
        return node.name if node.quoted else node.name.lower()
    return node.name


def _alias_key(name: str) -> str:
    return name.lower()
