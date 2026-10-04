"""Record what a table looks like *now*, keeping full history.

Ingestion adapters describe each table as an ``AssetObservation`` and call
``record_asset``. This module decides what changed:

- new table/column           -> identity row + first version
- unchanged                  -> only ``last_seen_at`` moves (no new version rows)
- structure changed          -> current version closed (``valid_to``), new version opened
- column no longer present   -> column marked deleted, its current version closed
- table/column reappears     -> same identity, ``deleted_at`` cleared, new version opened

Tables missing from a full scan are handled by ``mark_missing_assets``. This is the only
code that should write ``asset*`` rows, so the "one current version" invariant holds.
"""

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.graph.models import (
    Asset,
    AssetColumn,
    AssetColumnVersion,
    AssetKind,
    AssetVersion,
    IngestionRun,
    TypeFamily,
    utcnow,
)


@dataclass(frozen=True)
class ColumnObservation:
    name: str
    native_type: str
    type_family: TypeFamily
    is_nullable: bool
    ordinal_position: int
    default_expr: str | None = None
    comment: str | None = None


@dataclass(frozen=True)
class AssetObservation:
    namespace: str
    schema_name: str
    name: str
    kind: AssetKind
    columns: tuple[ColumnObservation, ...]
    primary_key: tuple[str, ...] = ()
    indexes: tuple[dict[str, Any], ...] = ()
    unique_constraints: tuple[dict[str, Any], ...] = ()
    foreign_keys: tuple[dict[str, Any], ...] = ()
    comment: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecordResult:
    asset_key: uuid.UUID
    asset_created: bool = False
    asset_reappeared: bool = False
    asset_changed: bool = False
    columns_added: list[str] = field(default_factory=list)
    columns_changed: list[str] = field(default_factory=list)
    columns_removed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return (
            self.asset_created
            or self.asset_reappeared
            or self.asset_changed
            or bool(self.columns_added or self.columns_changed or self.columns_removed)
        )


def record_asset(
    session: Session,
    run: IngestionRun,
    observation: AssetObservation,
    *,
    observed_at: datetime | None = None,
) -> RecordResult:
    """Record one table's current structure. Idempotent: recording the same observation
    twice creates no new versions."""
    now = observed_at or utcnow()
    _require_unique_column_names(observation)
    session.flush()  # make sure ``run`` has its id

    asset = session.scalar(
        select(Asset).where(
            Asset.tenant_id == run.tenant_id,
            Asset.data_source_id == run.data_source_id,
            Asset.namespace == observation.namespace,
            Asset.schema_name == observation.schema_name,
            Asset.name == observation.name,
        )
    )
    if asset is None:
        asset = Asset(
            asset_key=uuid.uuid4(),
            tenant_id=run.tenant_id,
            data_source_id=run.data_source_id,
            namespace=observation.namespace,
            schema_name=observation.schema_name,
            name=observation.name,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(asset)
        session.flush()
        result = RecordResult(asset_key=asset.asset_key, asset_created=True)
    else:
        result = RecordResult(asset_key=asset.asset_key)
        asset.last_seen_at = now
        if asset.deleted_at is not None:
            asset.deleted_at = None
            result.asset_reappeared = True

    _record_asset_version(session, run, asset, observation, now, result)
    _record_columns(session, run, asset, observation.columns, now, result)
    session.flush()
    return result


def mark_missing_assets(
    session: Session,
    run: IngestionRun,
    seen_asset_keys: Iterable[uuid.UUID],
    *,
    observed_at: datetime | None = None,
) -> list[uuid.UUID]:
    """After a **full** scan of a source, mark every table that wasn't seen as deleted
    (and its columns), closing their current versions. Returns the deleted asset keys.

    Don't call this after a partial scan, or the unscanned tables will look deleted.
    """
    now = observed_at or utcnow()
    seen = set(seen_asset_keys)
    candidates = session.scalars(
        select(Asset).where(
            Asset.tenant_id == run.tenant_id,
            Asset.data_source_id == run.data_source_id,
            Asset.deleted_at.is_(None),
        )
    ).all()
    deleted: list[uuid.UUID] = []
    for asset in candidates:
        if asset.asset_key in seen:
            continue
        asset.deleted_at = now
        _close(_current_asset_version(session, asset.asset_key), now)
        columns = session.scalars(
            select(AssetColumn).where(
                AssetColumn.asset_key == asset.asset_key, AssetColumn.deleted_at.is_(None)
            )
        ).all()
        for column in columns:
            column.deleted_at = now
            _close(_current_column_version(session, column.column_key), now)
        deleted.append(asset.asset_key)
    session.flush()
    return deleted


# --- internals -------------------------------------------------------------------------


def _record_asset_version(
    session: Session,
    run: IngestionRun,
    asset: Asset,
    observation: AssetObservation,
    now: datetime,
    result: RecordResult,
) -> None:
    state = _observed_asset_state(observation)
    current = _current_asset_version(session, asset.asset_key)
    if current is not None and _stored_asset_state(current) == state:
        return
    if current is not None:
        _close(current, now)
        session.flush()  # free the "one current version" slot before inserting
        if not result.asset_created:
            result.asset_changed = True
    session.add(
        AssetVersion(
            tenant_id=run.tenant_id,
            asset_key=asset.asset_key,
            ingestion_run_id=run.id,
            valid_from=now,
            **state,
        )
    )


def _record_columns(
    session: Session,
    run: IngestionRun,
    asset: Asset,
    observed: tuple[ColumnObservation, ...],
    now: datetime,
    result: RecordResult,
) -> None:
    existing = {
        column.name: column
        for column in session.scalars(
            select(AssetColumn).where(AssetColumn.asset_key == asset.asset_key)
        )
    }
    current_versions = {
        version.column_key: version
        for version in session.scalars(
            select(AssetColumnVersion)
            .join(AssetColumn, AssetColumn.column_key == AssetColumnVersion.column_key)
            .where(AssetColumn.asset_key == asset.asset_key, AssetColumnVersion.valid_to.is_(None))
        )
    }

    for col in observed:
        column = existing.get(col.name)
        is_new = column is None or column.deleted_at is not None
        if column is None:
            column = AssetColumn(
                column_key=uuid.uuid4(),
                tenant_id=run.tenant_id,
                asset_key=asset.asset_key,
                name=col.name,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(column)
            session.flush()
        else:
            column.last_seen_at = now
            column.deleted_at = None
        if is_new and not result.asset_created:
            result.columns_added.append(col.name)

        state = _observed_column_state(col)
        current = current_versions.get(column.column_key)
        if current is not None and _stored_column_state(current) == state:
            continue
        if current is not None:
            _close(current, now)
            session.flush()
            if not is_new:
                result.columns_changed.append(col.name)
        session.add(
            AssetColumnVersion(
                tenant_id=run.tenant_id,
                column_key=column.column_key,
                ingestion_run_id=run.id,
                valid_from=now,
                **state,
            )
        )

    observed_names = {col.name for col in observed}
    for name, column in existing.items():
        if name in observed_names or column.deleted_at is not None:
            continue
        column.deleted_at = now
        _close(current_versions.get(column.column_key), now)
        result.columns_removed.append(name)


def _current_asset_version(session: Session, asset_key: uuid.UUID) -> AssetVersion | None:
    return session.scalar(
        select(AssetVersion).where(
            AssetVersion.asset_key == asset_key, AssetVersion.valid_to.is_(None)
        )
    )


def _current_column_version(session: Session, column_key: uuid.UUID) -> AssetColumnVersion | None:
    return session.scalar(
        select(AssetColumnVersion).where(
            AssetColumnVersion.column_key == column_key, AssetColumnVersion.valid_to.is_(None)
        )
    )


def _close(version: AssetVersion | AssetColumnVersion | None, now: datetime) -> None:
    if version is None:
        return
    if now < version.valid_from:
        raise ValueError(
            f"observation at {now.isoformat()} is older than the current version "
            f"({version.valid_from.isoformat()}); record observations in time order"
        )
    version.valid_to = now


def _require_unique_column_names(observation: AssetObservation) -> None:
    names = [col.name for col in observation.columns]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate column names in observation of {observation.name!r}")


def _canonical(value: Any) -> Any:
    """JSON round-trip, so tuples vs lists or key order never look like a change."""
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _canonical_list(items: Iterable[Any]) -> list[Any]:
    """Canonical form for lists whose order is not meaningful (indexes, constraints)."""
    canonical = [_canonical(item) for item in items]
    return sorted(canonical, key=lambda item: json.dumps(item, sort_keys=True))


def _observed_asset_state(observation: AssetObservation) -> dict[str, Any]:
    return {
        "kind": observation.kind,
        "primary_key": list(observation.primary_key),  # column order matters for keys
        "indexes": _canonical_list(observation.indexes),
        "unique_constraints": _canonical_list(observation.unique_constraints),
        "foreign_keys": _canonical_list(observation.foreign_keys),
        "comment": observation.comment,
        "properties": _canonical(observation.properties),
    }


def _stored_asset_state(version: AssetVersion) -> dict[str, Any]:
    return {
        "kind": version.kind,
        "primary_key": list(version.primary_key),
        "indexes": _canonical_list(version.indexes),
        "unique_constraints": _canonical_list(version.unique_constraints),
        "foreign_keys": _canonical_list(version.foreign_keys),
        "comment": version.comment,
        "properties": _canonical(version.properties),
    }


def _observed_column_state(col: ColumnObservation) -> dict[str, Any]:
    return {
        "native_type": col.native_type,
        "type_family": col.type_family,
        "is_nullable": col.is_nullable,
        "ordinal_position": col.ordinal_position,
        "default_expr": col.default_expr,
        "comment": col.comment,
    }


def _stored_column_state(version: AssetColumnVersion) -> dict[str, Any]:
    return {
        "native_type": version.native_type,
        "type_family": version.type_family,
        "is_nullable": version.is_nullable,
        "ordinal_position": version.ordinal_position,
        "default_expr": version.default_expr,
        "comment": version.comment,
    }
