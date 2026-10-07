"""Which (child columns -> parent key) pairs are worth checking, and how they score.

Parents are keys: primary keys, unique constraints, keys from earlier versions (a legacy
table that lost its PK still has one logically), and single columns whose latest
measurement shows every value distinct. Children are columns of a compatible type that
pass cheap pruning on the measurements we already have, plus the gates below.
"""

from dataclasses import dataclass, field
from typing import Any

from ai_data_engineer.discovery.catalog import Catalog, ColumnInfo, TableInfo
from ai_data_engineer.discovery.naming import GENERIC_KEY_NAMES, name_score
from ai_data_engineer.discovery.settings import DiscoverySettings
from ai_data_engineer.graph.models import TypeFamily

# Only these families make sensible join keys.
KEY_FAMILIES = frozenset({TypeFamily.INTEGER, TypeFamily.STRING, TypeFamily.UUID})


@dataclass
class Candidate:
    child: TableInfo
    child_columns: tuple[str, ...]
    parent: TableInfo
    parent_columns: tuple[str, ...]
    name_score: float
    distinctive_values: bool  # text values distinctive enough to match on values alone
    key_source: str  # "primary_key" | "unique" | "historical_key" | "measured_unique"
    # Integer keys without a name match: only checked if the query log shows the join.
    needs_query_log: bool = False
    query_calls: int = 0
    inclusion: float | None = None
    values_checked: int | None = None
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def is_composite(self) -> bool:
        return len(self.child_columns) > 1

    @property
    def prior(self) -> float:
        """Ranking before any database query: cheapest-to-confirm, most likely first."""
        return (
            self.name_score
            + (0.5 if self.query_calls else 0.0)
            + (0.3 if self.distinctive_values else 0.0)
        )

    def evidence(self) -> dict[str, Any]:
        return {
            "inclusion": self.inclusion,
            "values_checked": self.values_checked,
            "name_score": round(self.name_score, 3),
            "query_log_calls": self.query_calls,
            "distinctive_values": self.distinctive_values,
            "parent_key": self.key_source,
            "reasons": self.reasons,
        }

    def signals(self) -> list[str]:
        out = ["value_inclusion"]
        if self.name_score:
            out.append("name_match")
        if self.query_calls:
            out.append("query_log_join")
        if self.distinctive_values:
            out.append("distinctive_values")
        return out


def parent_keys(table: TableInfo) -> list[tuple[tuple[str, ...], str]]:
    keys: list[tuple[tuple[str, ...], str]] = []
    if table.primary_key:
        keys.append((table.primary_key, "primary_key"))
    keys += [(k, "unique") for k in table.unique_keys if k != table.primary_key]
    keys += [
        (k, "historical_key") for k in table.historical_keys if all(c in table.columns for c in k)
    ]
    known = {k for k, _ in keys}
    for column in table.columns.values():
        if (
            (column.name,) not in known
            and column.family in KEY_FAMILIES
            and column.effectively_unique
        ):
            keys.append(((column.name,), "measured_unique"))
    return keys


def generate_candidates(
    catalog: Catalog,
    declared: set[tuple[str, tuple[str, ...]]],
    settings: DiscoverySettings,
) -> list[Candidate]:
    """``declared`` holds (child table ref, child columns) already covered by real FKs."""
    candidates: list[Candidate] = []
    for parent in catalog.tables.values():
        for key, key_source in parent_keys(parent):
            if any(parent.columns[c].family not in KEY_FAMILIES for c in key):
                continue
            for child in catalog.tables.values():
                if len(key) == 1:
                    candidates += _single_column(
                        child, parent, key[0], key_source, declared, settings
                    )
                else:
                    composite = _composite(child, parent, key, key_source, declared)
                    if composite is not None:
                        candidates.append(composite)
    return candidates


def _single_column(
    child: TableInfo,
    parent: TableInfo,
    parent_column: str,
    key_source: str,
    declared: set[tuple[str, tuple[str, ...]]],
    settings: DiscoverySettings,
) -> list[Candidate]:
    pkey = parent.columns[parent_column]
    found = []
    for column in child.columns.values():
        if child is parent and (column.name == parent_column or column.name in child.primary_key):
            # A table's own primary key doesn't reference another column of the same
            # table (real self-references look like parent_id -> id).
            continue
        if (child.ref, (column.name,)) in declared:
            continue
        if column.family is not pkey.family or column.name.lower() in GENERIC_KEY_NAMES:
            continue
        if not _plausible(column, pkey, settings):
            continue
        score = name_score(column.name, parent.name, parent_column)
        distinctive = _distinctive(column, pkey, settings)
        if column.family is TypeFamily.INTEGER and score < settings.min_name_score_for_integers:
            # Small 1..N integers "fit" inside every other ID column; without a name signal
            # this would be noise. (Query-log evidence can still rescue it, see discover.)
            found.append(
                _candidate(
                    child,
                    (column.name,),
                    parent,
                    (parent_column,),
                    score,
                    False,
                    key_source,
                    needs_query_log=True,
                )
            )
            continue
        if column.family is TypeFamily.STRING and score == 0.0 and not distinctive:
            continue
        found.append(
            _candidate(
                child, (column.name,), parent, (parent_column,), score, distinctive, key_source
            )
        )
    return found


def _composite(
    child: TableInfo,
    parent: TableInfo,
    key: tuple[str, ...],
    key_source: str,
    declared: set[tuple[str, tuple[str, ...]]],
) -> Candidate | None:
    """Composite keys are only matched by identical column names (exploring every column
    combination would explode on large schemas)."""
    if child is parent or (child.ref, key) in declared:
        return None
    if not all(
        c in child.columns and child.columns[c].family is parent.columns[c].family for c in key
    ):
        return None
    return _candidate(child, key, parent, key, 1.0, False, key_source)


def _candidate(
    child: TableInfo,
    child_columns: tuple[str, ...],
    parent: TableInfo,
    parent_columns: tuple[str, ...],
    score: float,
    distinctive: bool,
    key_source: str,
    *,
    needs_query_log: bool = False,
) -> Candidate:
    return Candidate(
        child,
        child_columns,
        parent,
        parent_columns,
        score,
        distinctive,
        key_source,
        needs_query_log=needs_query_log,
    )


def _plausible(child: ColumnInfo, parent: ColumnInfo, settings: DiscoverySettings) -> bool:
    """Cheap pruning with measurements we already have (no database query). Every rule
    here must be impossible for a real relationship to break, even one with orphans."""
    if child.distinct_count == 0:
        return False
    # Even at the minimum inclusion, the matched values must fit in the parent.
    if (
        child.distinct_count is not None
        and parent.distinct_count is not None
        and child.distinct_count * settings.min_inclusion > parent.distinct_count
    ):
        return False
    if child.family is TypeFamily.INTEGER and _numeric_range_outside(child, parent):
        return False
    if child.family is TypeFamily.STRING and child.avg_length and parent.avg_length:
        ratio = child.avg_length / parent.avg_length
        if abs(ratio - 1.0) > settings.length_tolerance:
            return False
    # Text min/max are NOT compared: the database orders text by its collation, which
    # Python's string ordering doesn't reproduce, so the check could reject true matches.
    return True


def _numeric_range_outside(child: ColumnInfo, parent: ColumnInfo) -> bool:
    try:
        cmin, cmax = float(child.min_repr or "nan"), float(child.max_repr or "nan")
        pmin, pmax = float(parent.min_repr or "nan"), float(parent.max_repr or "nan")
    except ValueError:
        return False
    # Comparisons with NaN are False, so missing extremes never prune.
    return cmax < pmin or cmin > pmax


def _distinctive(child: ColumnInfo, parent: ColumnInfo, settings: DiscoverySettings) -> bool:
    return (
        child.family in (TypeFamily.STRING, TypeFamily.UUID)
        and (parent.avg_length or 0) >= settings.min_distinctive_length
        and (child.distinct_count or 0) >= settings.min_child_distinct
    ) or child.family is TypeFamily.UUID
