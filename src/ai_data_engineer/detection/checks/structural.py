"""Structural checks, from the version history and current structure (no statistics)."""

import uuid
from typing import Any

from sqlalchemy import select

from ai_data_engineer.detection.context import DetectionContext, fingerprint
from ai_data_engineer.detection.framework import Check, CheckOutput, Observation
from ai_data_engineer.discovery.catalog import TableInfo
from ai_data_engineer.graph.models import (
    Asset,
    AssetColumn,
    AssetColumnVersion,
    AssetVersion,
    FindingCategory,
    Relationship,
    RelationshipStatus,
)

COLUMN_REMOVED = "column_removed"
TYPE_CHANGED = "type_changed"
NULLABILITY_CHANGED = "nullability_changed"
PRIMARY_KEY_REMOVED = "primary_key_removed"
MISSING_PRIMARY_KEY = "missing_primary_key"
UNINDEXED_FOREIGN_KEY = "unindexed_foreign_key"


def column_removed(ctx: DetectionContext) -> CheckOutput:
    out = CheckOutput()
    rows = ctx.session.execute(
        select(AssetColumn, Asset)
        .join(Asset, Asset.asset_key == AssetColumn.asset_key)
        .where(
            Asset.data_source_id == ctx.source.id,
            Asset.deleted_at.is_(None),
            AssetColumn.deleted_at.is_not(None),
            AssetColumn.deleted_at >= ctx.event_since,
        )
    ).all()
    for column, asset in rows:
        if column.deleted_at is None:  # excluded by the query; narrows the type
            continue
        ref = f"{asset.schema_name}.{asset.name}"
        out.observations.append(
            Observation(
                fingerprint=fingerprint(COLUMN_REMOVED, column.column_key, column.deleted_at),
                category=FindingCategory.STRUCTURAL,
                severity=ctx.settings.severity_column_removed,
                title=f"Column {ref}.{column.name} was removed",
                description=(
                    f"{ref}.{column.name} existed until the scan at "
                    f"{column.deleted_at.isoformat()} and is gone now. Anything reading it "
                    "(reports, ETL, application code) will fail or get NULLs."
                ),
                evidence={"removed_at": column.deleted_at.isoformat(), "table": ref},
                asset_key=asset.asset_key,
                column_key=column.column_key,
            )
        )
    return out


def type_changed(ctx: DetectionContext) -> CheckOutput:
    out = CheckOutput()
    for new, old, column, asset in _column_changes(ctx):
        if (new.native_type, new.type_family) == (old.native_type, old.type_family):
            continue
        family_changed = new.type_family is not old.type_family
        ref = f"{asset.schema_name}.{asset.name}.{column.name}"
        out.observations.append(
            Observation(
                fingerprint=fingerprint(TYPE_CHANGED, new.id),
                category=FindingCategory.STRUCTURAL,
                severity=(
                    ctx.settings.severity_type_family_changed
                    if family_changed
                    else ctx.settings.severity_type_detail_changed
                ),
                title=f"Type of {ref} changed: {old.native_type} -> {new.native_type}",
                description=(
                    f"{ref} changed from {old.native_type} ({old.type_family.value}) to "
                    f"{new.native_type} ({new.type_family.value}) at {new.valid_from.isoformat()}."
                    + (" The kind of data changed; consumers may break." if family_changed else "")
                ),
                evidence={
                    "before": old.native_type,
                    "after": new.native_type,
                    "family_before": old.type_family.value,
                    "family_after": new.type_family.value,
                    "changed_at": new.valid_from.isoformat(),
                },
                asset_key=asset.asset_key,
                column_key=column.column_key,
            )
        )
    return out


def nullability_changed(ctx: DetectionContext) -> CheckOutput:
    out = CheckOutput()
    for new, old, column, asset in _column_changes(ctx):
        if new.is_nullable == old.is_nullable:
            continue
        ref = f"{asset.schema_name}.{asset.name}.{column.name}"
        now_allows = "now allows" if new.is_nullable else "no longer allows"
        out.observations.append(
            Observation(
                fingerprint=fingerprint(NULLABILITY_CHANGED, new.id),
                category=FindingCategory.STRUCTURAL,
                severity=ctx.settings.severity_nullability_changed,
                title=f"{ref} {now_allows} NULLs",
                description=f"The NOT NULL rule on {ref} changed at {new.valid_from.isoformat()}.",
                evidence={"nullable_before": old.is_nullable, "nullable_after": new.is_nullable},
                asset_key=asset.asset_key,
                column_key=column.column_key,
            )
        )
    return out


def primary_key_removed(ctx: DetectionContext) -> CheckOutput:
    out = CheckOutput()
    for new, old, asset in _asset_changes(ctx):
        if not old.primary_key or new.primary_key:
            continue
        ref = f"{asset.schema_name}.{asset.name}"
        out.observations.append(
            Observation(
                fingerprint=fingerprint(PRIMARY_KEY_REMOVED, new.id),
                category=FindingCategory.STRUCTURAL,
                severity=ctx.settings.severity_primary_key_removed,
                title=f"{ref} lost its primary key ({', '.join(old.primary_key)})",
                description=(
                    f"{ref} had primary key ({', '.join(old.primary_key)}) until "
                    f"{new.valid_from.isoformat()}. Duplicate rows can now be inserted, and "
                    "lookups by that key lose their index."
                ),
                evidence={
                    "previous_key": old.primary_key,
                    "changed_at": new.valid_from.isoformat(),
                },
                asset_key=asset.asset_key,
            )
        )
    return out


def missing_primary_key(ctx: DetectionContext) -> CheckOutput:
    """Tables that have never had a primary key (a lost key is ``primary_key_removed``)."""
    out = CheckOutput()
    for table in ctx.catalog.tables.values():
        if table.kind != "table":
            continue
        fp = fingerprint(MISSING_PRIMARY_KEY, table.asset_key)
        out.evaluated.add(fp)
        if table.primary_key or table.historical_keys:
            continue
        out.observations.append(
            Observation(
                fingerprint=fp,
                category=FindingCategory.STRUCTURAL,
                severity=ctx.settings.severity_missing_primary_key,
                title=f"{table.ref} has no primary key",
                description=(
                    f"{table.ref} has no primary key, so nothing stops duplicate rows and "
                    "individual rows can't be reliably identified or referenced."
                ),
                evidence={"rows": table.estimated_rows, "columns": list(table.columns)},
                asset_key=table.asset_key,
            )
        )
    return out


def unindexed_foreign_key(ctx: DetectionContext) -> CheckOutput:
    """Relationship columns without an index: joins and parent deletes scan the table."""
    out = CheckOutput()
    by_key = {
        column.column_key: (table, column.name)
        for table in ctx.catalog.tables.values()
        for column in table.columns.values()
    }
    for rel in ctx.relationships:
        if rel.status is RelationshipStatus.REJECTED:
            continue
        child = _child_side(rel, by_key)
        if child is None:
            continue
        table, columns = child
        fp = fingerprint(UNINDEXED_FOREIGN_KEY, rel.signature)
        out.evaluated.add(fp)
        if _indexed(columns, table.indexes):
            continue
        ref = f"{table.ref}({', '.join(columns)})"
        out.observations.append(
            Observation(
                fingerprint=fp,
                category=FindingCategory.STRUCTURAL,
                severity=ctx.settings.severity_unindexed_foreign_key,
                title=f"{ref} references another table but has no index",
                description=(
                    f"{ref} is a {rel.kind.value} relationship column without an index: "
                    "joins through it and deletes on the parent table scan the whole table."
                ),
                evidence={
                    "relationship_status": rel.status.value,
                    "relationship_confidence": rel.confidence,
                },
                asset_key=table.asset_key,
                column_key=rel.columns[0].from_column_key if len(columns) == 1 else None,
                relationship_id=rel.id,
            )
        )
    return out


def _child_side(
    rel: Relationship, by_key: dict[uuid.UUID, tuple[TableInfo, str]]
) -> tuple[TableInfo, list[str]] | None:
    """The child table and column names of a relationship, if they all still exist."""
    columns: list[str] = []
    table: TableInfo | None = None
    for pair in rel.columns:
        side = by_key.get(pair.from_column_key)
        if side is None:
            return None
        table = side[0]
        columns.append(side[1])
    return (table, columns) if table is not None else None


def _indexed(columns: list[str], indexes: tuple[dict[str, Any], ...]) -> bool:
    """An index helps when its leading columns are exactly the relationship's columns."""
    wanted = set(columns)
    return any(set(ix.get("columns", [])[: len(columns)]) == wanted for ix in indexes)


def _column_changes(
    ctx: DetectionContext,
) -> list[tuple[AssetColumnVersion, AssetColumnVersion, AssetColumn, Asset]]:
    """Recent column versions together with the version they replaced."""
    rows = ctx.session.execute(
        select(AssetColumnVersion, AssetColumn, Asset)
        .join(AssetColumn, AssetColumn.column_key == AssetColumnVersion.column_key)
        .join(Asset, Asset.asset_key == AssetColumn.asset_key)
        .where(
            Asset.data_source_id == ctx.source.id, AssetColumnVersion.valid_from >= ctx.event_since
        )
    ).all()
    changes = []
    for new, column, asset in rows:
        old = ctx.session.scalar(
            select(AssetColumnVersion).where(
                AssetColumnVersion.column_key == new.column_key,
                AssetColumnVersion.valid_to == new.valid_from,
            )
        )
        if old is not None:
            changes.append((new, old, column, asset))
    return changes


def _asset_changes(ctx: DetectionContext) -> list[tuple[AssetVersion, AssetVersion, Asset]]:
    rows = ctx.session.execute(
        select(AssetVersion, Asset)
        .join(Asset, Asset.asset_key == AssetVersion.asset_key)
        .where(Asset.data_source_id == ctx.source.id, AssetVersion.valid_from >= ctx.event_since)
    ).all()
    changes = []
    for new, asset in rows:
        old = ctx.session.scalar(
            select(AssetVersion).where(
                AssetVersion.asset_key == new.asset_key, AssetVersion.valid_to == new.valid_from
            )
        )
        if old is not None:
            changes.append((new, old, asset))
    return changes


CHECKS = [
    Check(COLUMN_REMOVED, "event", "a column disappeared", column_removed),
    Check(TYPE_CHANGED, "event", "a column's type changed", type_changed),
    Check(NULLABILITY_CHANGED, "event", "a column's NOT NULL rule changed", nullability_changed),
    Check(PRIMARY_KEY_REMOVED, "event", "a table lost its primary key", primary_key_removed),
    Check(
        MISSING_PRIMARY_KEY, "condition", "a table has never had a primary key", missing_primary_key
    ),
    Check(
        UNINDEXED_FOREIGN_KEY,
        "condition",
        "a relationship column has no index",
        unindexed_foreign_key,
    ),
]
