"""Judging the counts (pure, no database): which columns hold which kind of personal
data, which hold some where their name doesn't say so, and which columns hold the same
kind under different names."""

from collections import defaultdict
from dataclasses import dataclass

from lumida.ingestion.base import PiiCounts
from lumida.semantic.patterns import expected_kinds
from lumida.semantic.settings import SemanticSettings


@dataclass(frozen=True)
class ColumnPii:
    table: str  # "schema.table"
    column: str
    kind: str
    matching: int
    checked: int  # non-NULL values checked
    expected: bool  # the column's name announces this kind

    @property
    def share(self) -> float:
        return self.matching / self.checked if self.checked else 0.0


def classify(table: str, counts: PiiCounts) -> list[ColumnPii]:
    """Every (column, kind) with at least one matching value."""
    out = []
    for column, by_kind in counts.matches.items():
        checked = counts.non_null.get(column, 0)
        announced = expected_kinds(column)
        for kind, matching in by_kind.items():
            if matching:
                out.append(ColumnPii(table, column, kind, matching, checked, kind in announced))
    return out


def holds(found: list[ColumnPii], s: SemanticSettings) -> list[ColumnPii]:
    """Columns that hold a kind (most of their values look like it): the PII map."""
    return [f for f in found if f.share >= s.holds_share]


def unexpected(found: list[ColumnPii], s: SemanticSettings) -> list[ColumnPii]:
    """Personal data in a column whose name doesn't announce it."""
    return [
        f
        for f in found
        if not f.expected
        and f.matching >= s.unexpected_min_values
        and f.share >= s.unexpected_min_share
    ]


def same_kind_different_names(mapped: list[ColumnPii]) -> dict[str, list[str]]:
    """Kind -> "schema.table.column" for kinds held under more than one column name
    (``email``, ``E_MAIL``, ``contact_addr``): one concept, several names."""
    names: dict[str, set[str]] = defaultdict(set)
    where: dict[str, list[str]] = defaultdict(list)
    for f in mapped:
        names[f.kind].add(f.column.lower())
        where[f.kind].append(f"{f.table}.{f.column}")
    return {kind: sorted(where[kind]) for kind in names if len(names[kind]) > 1}
