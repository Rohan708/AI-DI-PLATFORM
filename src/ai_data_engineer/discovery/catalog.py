"""A read-only snapshot of what the metadata store knows about one source, shaped for
discovery: tables, columns with their latest measurements, keys (current and historical),
and declared foreign keys."""

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.graph.models import (
    Asset,
    AssetVersion,
    DataSource,
    TypeFamily,
)
from ai_data_engineer.graph.queries import (
    get_asset_profile_history,
    get_column_profile_history,
    get_current_structure,
)


@dataclass(frozen=True)
class ColumnInfo:
    column_key: uuid.UUID
    name: str
    family: TypeFamily
    native_type: str
    row_count: int | None = None
    null_count: int | None = None
    distinct_count: int | None = None
    avg_length: float | None = None
    min_repr: str | None = None
    max_repr: str | None = None

    @property
    def non_null(self) -> int | None:
        if self.row_count is None or self.null_count is None:
            return None
        return self.row_count - self.null_count

    @property
    def effectively_unique(self) -> bool:
        """Every non-null value is distinct (per the latest measurement)."""
        return (
            self.distinct_count is not None
            and self.non_null is not None
            and self.non_null > 0
            and self.distinct_count == self.non_null
        )


@dataclass(frozen=True)
class TableInfo:
    asset_key: uuid.UUID
    schema_name: str
    name: str
    columns: dict[str, ColumnInfo]
    primary_key: tuple[str, ...]
    unique_keys: tuple[tuple[str, ...], ...]  # declared unique constraints
    historical_keys: tuple[tuple[str, ...], ...]  # PKs that existed in earlier versions
    foreign_keys: tuple[dict[str, Any], ...]
    estimated_rows: int | None = None
    indexes: tuple[dict[str, Any], ...] = ()
    kind: str = "table"

    @property
    def ref(self) -> str:
        return f"{self.schema_name}.{self.name}"

    def declared_keys(self) -> list[tuple[str, ...]]:
        keys = [self.primary_key] if self.primary_key else []
        return keys + [k for k in self.unique_keys if k not in keys]


@dataclass
class Catalog:
    source: DataSource
    tables: dict[str, TableInfo] = field(default_factory=dict)  # by "schema.table"

    def find(self, schema: str, table: str) -> TableInfo | None:
        """Look a table up; an empty schema matches a uniquely named table in any schema."""
        if schema:
            exact = self.tables.get(f"{schema}.{table}")
            if exact is not None:
                return exact
            # Query logs may spell names in another case (MySQL, SQL Server).
            matches = [
                t
                for t in self.tables.values()
                if t.schema_name.lower() == schema.lower() and t.name.lower() == table.lower()
            ]
        else:
            matches = [t for t in self.tables.values() if t.name == table]
            if not matches:
                matches = [t for t in self.tables.values() if t.name.lower() == table.lower()]
        return matches[0] if len(matches) == 1 else None


def load_catalog(session: Session, source: DataSource) -> Catalog:
    catalog = Catalog(source=source)
    for asset in get_current_structure(session, source.id):
        current, history = _versions(session, asset.asset_key)
        columns = {}
        for column in asset.columns:
            profiles = get_column_profile_history(session, column.column_key, limit=1)
            p = profiles[0] if profiles else None
            columns[column.name] = ColumnInfo(
                column_key=column.column_key,
                name=column.name,
                family=column.type_family,
                native_type=column.native_type,
                row_count=p.row_count if p else None,
                null_count=p.null_count if p else None,
                distinct_count=p.distinct_count if p else None,
                avg_length=p.avg_length if p else None,
                min_repr=p.min_repr if p else None,
                max_repr=p.max_repr if p else None,
            )
        latest = get_asset_profile_history(session, asset.asset_key, limit=1)
        table = TableInfo(
            asset_key=asset.asset_key,
            schema_name=asset.schema_name,
            name=asset.name,
            columns=columns,
            primary_key=asset.primary_key,
            unique_keys=tuple(tuple(u["columns"]) for u in current.unique_constraints),
            historical_keys=tuple(
                dict.fromkeys(
                    tuple(v.primary_key)
                    for v in history
                    if v.primary_key and tuple(v.primary_key) != asset.primary_key
                )
            ),
            foreign_keys=tuple(current.foreign_keys),
            estimated_rows=latest[0].row_count if latest else None,
            indexes=tuple(current.indexes),
            kind=asset.kind.value,
        )
        catalog.tables[table.ref] = table
    return catalog


def _versions(session: Session, asset_key: uuid.UUID) -> tuple[AssetVersion, list[AssetVersion]]:
    versions = list(
        session.scalars(
            select(AssetVersion)
            .join(Asset, Asset.asset_key == AssetVersion.asset_key)
            .where(AssetVersion.asset_key == asset_key)
            .order_by(AssetVersion.valid_from)
        )
    )
    current = next(v for v in versions if v.valid_to is None)
    return current, [v for v in versions if v.valid_to is not None]
