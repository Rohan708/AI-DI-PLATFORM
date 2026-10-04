"""The current structure of a data source: live tables with their current columns."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.graph.models import (
    Asset,
    AssetColumn,
    AssetColumnVersion,
    AssetKind,
    AssetVersion,
    TypeFamily,
)


@dataclass(frozen=True)
class CurrentColumn:
    column_key: uuid.UUID
    name: str
    native_type: str
    type_family: TypeFamily
    is_nullable: bool
    ordinal_position: int


@dataclass(frozen=True)
class CurrentAsset:
    asset_key: uuid.UUID
    namespace: str
    schema_name: str
    name: str
    kind: AssetKind
    primary_key: tuple[str, ...]
    columns: tuple[CurrentColumn, ...]


def get_current_structure(session: Session, data_source_id: uuid.UUID) -> list[CurrentAsset]:
    """Non-deleted assets of a source with their current columns, ordered by
    schema, name, and column position."""
    assets = session.execute(
        select(Asset, AssetVersion)
        .join(AssetVersion, AssetVersion.asset_key == Asset.asset_key)
        .where(
            Asset.data_source_id == data_source_id,
            Asset.deleted_at.is_(None),
            AssetVersion.valid_to.is_(None),
        )
        .order_by(Asset.schema_name, Asset.name)
    ).all()

    columns_by_asset: dict[uuid.UUID, list[CurrentColumn]] = {}
    rows = session.execute(
        select(AssetColumn, AssetColumnVersion)
        .join(AssetColumnVersion, AssetColumnVersion.column_key == AssetColumn.column_key)
        .join(Asset, Asset.asset_key == AssetColumn.asset_key)
        .where(
            Asset.data_source_id == data_source_id,
            AssetColumn.deleted_at.is_(None),
            AssetColumnVersion.valid_to.is_(None),
        )
        .order_by(AssetColumnVersion.ordinal_position, AssetColumn.name)
    ).all()
    for column, version in rows:
        columns_by_asset.setdefault(column.asset_key, []).append(
            CurrentColumn(
                column_key=column.column_key,
                name=column.name,
                native_type=version.native_type,
                type_family=version.type_family,
                is_nullable=version.is_nullable,
                ordinal_position=version.ordinal_position,
            )
        )

    return [
        CurrentAsset(
            asset_key=asset.asset_key,
            namespace=asset.namespace,
            schema_name=asset.schema_name,
            name=asset.name,
            kind=version.kind,
            primary_key=tuple(version.primary_key),
            columns=tuple(columns_by_asset.get(asset.asset_key, [])),
        )
        for asset, version in assets
    ]
