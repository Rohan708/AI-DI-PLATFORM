"""Checks that need relationships: orphan rows and non-unique parent keys.

- **orphan_rows**: child rows whose key has no parent row ("148 orders point to customers
  that don't exist"), with identifiers of some offending rows. Runs on confirmed
  relationships and on proposed ones at/above ``orphan_check_min_confidence``. Declared
  FKs are skipped: the database already enforces them.
- **parent_key_not_unique**: the parent side of a relationship has duplicate values, so
  joins fan out and totals double-count. Uses the latest measurements (no extra query).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from ai_data_engineer.detection.recording import latest_run_id, record_finding, resolve_finding
from ai_data_engineer.discovery.catalog import Catalog, ColumnInfo, TableInfo
from ai_data_engineer.discovery.settings import DiscoverySettings
from ai_data_engineer.graph.models import (
    FindingCategory,
    Relationship,
    RelationshipKind,
    RelationshipStatus,
    Severity,
    finding_fingerprint,
)
from ai_data_engineer.ingestion.base import KeyRef, SourceAdapter

ORPHAN_CHECK = "orphan_rows"
PARENT_UNIQUE_CHECK = "parent_key_not_unique"


@dataclass
class CheckResult:
    opened: int = 0
    refreshed: int = 0
    resolved: int = 0
    checked: int = 0


def run_relationship_checks(
    session: Session,
    catalog: Catalog,
    relationships: list[Relationship],
    adapter: SourceAdapter,
    now: datetime,
    settings: DiscoverySettings,
) -> CheckResult:
    result = CheckResult()
    run_id = latest_run_id(session, catalog.source)
    by_key = {
        column.column_key: (table, column)
        for table in catalog.tables.values()
        for column in table.columns.values()
    }
    for rel in relationships:
        if rel.status is RelationshipStatus.REJECTED or rel.kind is RelationshipKind.DECLARED:
            continue
        sides = _resolve_sides(rel, by_key)
        if sides is None:
            continue  # a column was dropped since; nothing to check
        child_table, child_cols, parent_table, parent_cols = sides
        result.checked += 1
        _check_parent_unique(session, catalog, rel, parent_table, parent_cols, now, run_id, result)
        trusted = rel.status is RelationshipStatus.CONFIRMED or (
            rel.confidence is not None and rel.confidence >= settings.orphan_check_min_confidence
        )
        if trusted:
            _check_orphans(
                session, catalog, rel, adapter, child_table, child_cols, parent_table,
                parent_cols, now, run_id, settings, result,
            )  # fmt: skip
    return result


def _check_orphans(
    session: Session,
    catalog: Catalog,
    rel: Relationship,
    adapter: SourceAdapter,
    child: TableInfo,
    child_cols: list[ColumnInfo],
    parent: TableInfo,
    parent_cols: list[ColumnInfo],
    now: datetime,
    run_id: uuid.UUID | None,
    settings: DiscoverySettings,
    result: CheckResult,
) -> None:
    fingerprint = finding_fingerprint(ORPHAN_CHECK, rel.signature)
    orphans = adapter.count_orphans(
        KeyRef(
            child.schema_name, child.name, tuple(c.name for c in child_cols), child.estimated_rows
        ),
        KeyRef(parent.schema_name, parent.name, tuple(c.name for c in parent_cols)),
        row_ids=child.primary_key,
        sample_size=settings.orphan_sample_size,
    )
    if orphans.orphan_rows == 0:
        if resolve_finding(session, catalog.source.tenant_id, fingerprint, now):
            result.resolved += 1
        return

    fraction = orphans.orphan_rows / orphans.checked_rows
    link = _describe(child, child_cols, parent, parent_cols)
    basis = (
        "a confirmed relationship"
        if rel.status is RelationshipStatus.CONFIRMED
        else f"a proposed relationship (confidence {rel.confidence:.2f}, not yet reviewed)"
    )
    _, created = record_finding(
        session,
        source=catalog.source,
        fingerprint=fingerprint,
        category=FindingCategory.RELATIONAL,
        check_name=ORPHAN_CHECK,
        severity=Severity.HIGH
        if fraction >= settings.orphan_high_severity_fraction
        else Severity.MEDIUM,
        title=f"{orphans.orphan_rows:,} rows in {child.ref} point to missing {parent.ref} rows",
        description=(
            f"{orphans.orphan_rows:,} of {orphans.checked_rows:,} rows ({fraction:.2%}) in "
            f"{child.ref} have a {', '.join(c.name for c in child_cols)} value with no matching "
            f"row in {parent.ref}. Based on {basis}: {link}."
        ),
        evidence={
            "relationship": link,
            "relationship_status": rel.status.value,
            "relationship_confidence": rel.confidence,
            "orphan_rows": orphans.orphan_rows,
            "checked_rows": orphans.checked_rows,
            "orphan_fraction": round(fraction, 6),
            "sampled": orphans.sampled,
            "sample_rows": orphans.sample,
            "sample_identifies_by": list(child.primary_key or tuple(c.name for c in child_cols)),
        },
        now=now,
        run_id=run_id,
        asset_key=child.asset_key,
        column_key=child_cols[0].column_key if len(child_cols) == 1 else None,
        relationship_id=rel.id,
    )
    if created:
        result.opened += 1
    else:
        result.refreshed += 1


def _check_parent_unique(
    session: Session,
    catalog: Catalog,
    rel: Relationship,
    parent: TableInfo,
    parent_cols: list[ColumnInfo],
    now: datetime,
    run_id: uuid.UUID | None,
    result: CheckResult,
) -> None:
    if len(parent_cols) != 1:
        return  # composite uniqueness isn't measured per column
    column = parent_cols[0]
    fingerprint = finding_fingerprint(PARENT_UNIQUE_CHECK, column.column_key)
    if column.distinct_count is None or column.non_null is None:
        return
    duplicates = column.non_null - column.distinct_count
    if duplicates <= 0:
        if resolve_finding(session, catalog.source.tenant_id, fingerprint, now):
            result.resolved += 1
        return
    _, created = record_finding(
        session,
        source=catalog.source,
        fingerprint=fingerprint,
        category=FindingCategory.RELATIONAL,
        check_name=PARENT_UNIQUE_CHECK,
        severity=Severity.HIGH,
        title=f"{parent.ref}.{column.name} is referenced as a key but has duplicates",
        description=(
            f"{column.non_null:,} values but only {column.distinct_count:,} distinct in "
            f"{parent.ref}.{column.name}; joins through it multiply rows."
        ),
        evidence={
            "non_null_values": column.non_null,
            "distinct_values": column.distinct_count,
            "duplicate_values": duplicates,
            "relationship_id": str(rel.id),
        },
        now=now,
        run_id=run_id,
        asset_key=parent.asset_key,
        column_key=column.column_key,
        relationship_id=rel.id,
    )
    if created:
        result.opened += 1
    else:
        result.refreshed += 1


Sides = tuple[TableInfo, list[ColumnInfo], TableInfo, list[ColumnInfo]]


def _resolve_sides(
    rel: Relationship, by_key: dict[uuid.UUID, tuple[TableInfo, ColumnInfo]]
) -> Sides | None:
    child_cols: list[ColumnInfo] = []
    parent_cols: list[ColumnInfo] = []
    child_table: TableInfo | None = None
    parent_table: TableInfo | None = None
    for pair in rel.columns:
        child = by_key.get(pair.from_column_key)
        parent = by_key.get(pair.to_column_key)
        if child is None or parent is None:
            return None
        child_table, parent_table = child[0], parent[0]
        child_cols.append(child[1])
        parent_cols.append(parent[1])
    if child_table is None or parent_table is None:
        return None
    return child_table, child_cols, parent_table, parent_cols


def _describe(
    child: TableInfo, child_cols: list[ColumnInfo], parent: TableInfo, parent_cols: list[ColumnInfo]
) -> str:
    return (
        f"{child.ref}({', '.join(c.name for c in child_cols)}) -> "
        f"{parent.ref}({', '.join(c.name for c in parent_cols)})"
    )
