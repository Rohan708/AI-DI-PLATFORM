"""The rule format: a small, safe vocabulary of business rules that compiles to read-only
SQL. Rules are data (JSON), never SQL text, so an AI-proposed rule can be reviewed by a
person and can't do anything except count rows.

Three kinds (each "row" is one row of ``table``):

- ``compare_columns``: ``column <op> other_column``, both in the same row, or
  ``other_column`` in a parent table reached through a relationship (``via``), e.g.
  ``shop.shipments.shipped_at >= shop.orders.order_date`` via ``ord_id -> order_id``.
- ``compare_constant``: ``column <op> value`` for a number, e.g. ``quantity > 0``.
- ``sum_matches``: ``column`` equals the sum of ``child_column`` over the child rows that
  point to this row (within ``tolerance``), e.g. an invoice total and its lines.

Rows where a compared value is NULL are not judged (unknown is not a violation). Every
join must follow a relationship the engine knows about, so a rule can't fan out rows.
"""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ai_data_engineer.discovery.catalog import Catalog, ColumnInfo, TableInfo
from ai_data_engineer.graph.models import TypeFamily

Op = Literal["<", "<=", "=", ">=", ">", "<>"]

NUMERIC = frozenset({TypeFamily.INTEGER, TypeFamily.DECIMAL, TypeFamily.FLOAT})
TEMPORAL = frozenset({TypeFamily.DATE, TypeFamily.TIMESTAMP})


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Via(_Spec):
    """How a row reaches its parent: pairs of (column in the table, column in the parent)."""

    parent_table: str
    on: list[tuple[str, str]] = Field(min_length=1)


class CompareColumns(_Spec):
    kind: Literal["compare_columns"] = "compare_columns"
    table: str
    column: str
    op: Op
    other_column: str
    via: Via | None = None  # other_column is in via.parent_table


class CompareConstant(_Spec):
    kind: Literal["compare_constant"] = "compare_constant"
    table: str
    column: str
    op: Op
    value: float


class SumMatches(_Spec):
    kind: Literal["sum_matches"] = "sum_matches"
    table: str  # the parent, e.g. the invoice header
    column: str  # its total
    child_table: str  # e.g. the invoice lines
    child_column: str  # summed
    on: list[tuple[str, str]] = Field(min_length=1)  # (child column, parent column)
    tolerance: float = Field(default=0.01, ge=0)  # rounding


RuleSpec = Annotated[CompareColumns | CompareConstant | SumMatches, Field(discriminator="kind")]
RULE_SPEC: TypeAdapter[RuleSpec] = TypeAdapter(RuleSpec)
RULE_KINDS = ("compare_columns", "compare_constant", "sum_matches")

# A known relationship, as (child table, child columns, parent table, parent columns).
Link = tuple[str, tuple[str, ...], str, tuple[str, ...]]


def parse_spec(data: object) -> RuleSpec:
    return RULE_SPEC.validate_python(data)


def spec_json(spec: RuleSpec) -> dict[str, object]:
    return spec.model_dump(mode="json")


def signature(spec: RuleSpec) -> str:
    """Stable identity of a rule, so the same rule is never proposed twice."""
    canonical = json.dumps(spec_json(spec), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def describe(spec: RuleSpec) -> str:
    """One line a person can review, e.g. ``shop.shipments.shipped_at >= shop.orders.order_date
    (via ord_id -> order_id)``."""
    match spec:
        case CompareColumns(via=None):
            return f"{spec.table}.{spec.column} {spec.op} {spec.other_column}"
        case CompareColumns(via=Via() as via):
            joins = ", ".join(f"{a} -> {b}" for a, b in via.on)
            return (
                f"{spec.table}.{spec.column} {spec.op} {via.parent_table}.{spec.other_column}"
                f" (via {joins})"
            )
        case CompareConstant():
            return f"{spec.table}.{spec.column} {spec.op} {spec.value:g}"
        case SumMatches():
            joins = ", ".join(f"{a} -> {b}" for a, b in spec.on)
            return (
                f"{spec.table}.{spec.column} = sum({spec.child_table}.{spec.child_column})"
                f" (via {joins}, tolerance {spec.tolerance:g})"
            )
    raise AssertionError(spec)  # pragma: no cover


def subject(spec: RuleSpec) -> tuple[str, str]:
    """The (table, column) a violation is reported on."""
    return spec.table, spec.column


def validate(spec: RuleSpec, catalog: Catalog, links: list[Link] | None = None) -> list[str]:
    """Problems that stop the rule from running on this source (empty = fine). With
    ``links``, every join must match one of these relationships (used for proposals;
    an approved rule is trusted to keep its join)."""
    problems: list[str] = []
    table = _table(catalog, spec.table, problems)
    if table is None:
        return problems
    column = _column(table, spec.column, problems)
    match spec:
        case CompareColumns(via=None):
            other = _column(table, spec.other_column, problems)
            _comparable(column, other, problems)
        case CompareColumns(via=Via() as via):
            parent = _table(catalog, via.parent_table, problems)
            if parent is not None:
                _join(table, parent, via.on, problems, links)
                other = _column(parent, spec.other_column, problems)
                _comparable(column, other, problems)
        case CompareConstant():
            if column is not None and column.family not in NUMERIC:
                problems.append(f"{spec.table}.{spec.column} is not numeric")
        case SumMatches():
            child = _table(catalog, spec.child_table, problems)
            if child is not None:
                _join(child, table, spec.on, problems, links)
                summed = _column(child, spec.child_column, problems)
                for c in (column, summed):
                    if c is not None and c.family not in NUMERIC:
                        problems.append(f"{c.name} is not numeric")
    return problems


def _table(catalog: Catalog, ref: str, problems: list[str]) -> TableInfo | None:
    table = catalog.tables.get(ref)
    if table is None:
        problems.append(f"unknown table {ref}")
    return table


def _column(table: TableInfo, name: str, problems: list[str]) -> ColumnInfo | None:
    column = table.columns.get(name)
    if column is None:
        problems.append(f"unknown column {table.ref}.{name}")
    return column


def _comparable(a: ColumnInfo | None, b: ColumnInfo | None, problems: list[str]) -> None:
    if a is None or b is None:
        return
    same_group = any(a.family in g and b.family in g for g in (NUMERIC, TEMPORAL))
    if not same_group and a.family != b.family:
        problems.append(
            f"{a.name} ({a.family.value}) and {b.name} ({b.family.value}) can't be compared"
        )


def _join(
    child: TableInfo,
    parent: TableInfo,
    on: list[tuple[str, str]],
    problems: list[str],
    links: list[Link] | None,
) -> None:
    for child_col, parent_col in on:
        _column(child, child_col, problems)
        _column(parent, parent_col, problems)
    if links is None:
        return
    pairs = sorted(on)
    for link_child, child_cols, link_parent, parent_cols in links:
        if (link_child, link_parent) == (child.ref, parent.ref) and pairs == sorted(
            zip(child_cols, parent_cols, strict=True)
        ):
            return
    joins = ", ".join(f"{a} -> {b}" for a, b in on)
    problems.append(f"no known relationship {child.ref}({joins}) -> {parent.ref}")
