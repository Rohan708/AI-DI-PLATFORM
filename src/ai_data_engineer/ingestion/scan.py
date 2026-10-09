"""One scan of a data source = one ingestion run.

1. open a run
2. read the structure and record it through ``graph.versioning`` (new versions only on
   real changes); tables that disappeared are marked deleted
3. profile each table/materialized view and store the measurements
4. close the run: ``succeeded``, ``partial`` (some tables failed; the rest are kept) or
   ``failed`` (couldn't read the structure at all)

The caller owns the transaction: nothing here commits.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ai_data_engineer.graph.models import (
    AssetColumn,
    AssetProfile,
    ColumnProfile,
    DataSource,
    IngestionRun,
    RunStatus,
    SourceKind,
    utcnow,
)
from ai_data_engineer.graph.versioning import mark_missing_assets, record_asset
from ai_data_engineer.ingestion.base import DiscoveredTable, SourceAdapter, TableMeasurement
from ai_data_engineer.ingestion.connections import resolve_connection_url
from ai_data_engineer.ingestion.sources import scan_settings

# Error messages stored on the run are truncated (they can be long and noisy).
MAX_ERROR_LENGTH = 500

AdapterFactory = Callable[[DataSource], SourceAdapter]


@dataclass
class ScanResult:
    run_id: uuid.UUID
    status: RunStatus
    tables_seen: int = 0
    tables_profiled: int = 0
    tables_created: list[str] = field(default_factory=list)
    tables_changed: list[str] = field(default_factory=list)
    tables_removed: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        line = (
            f"{self.status.value}: {self.tables_seen} tables seen, {self.tables_profiled} profiled"
        )
        if self.tables_created:
            line += f", {len(self.tables_created)} new"
        if self.tables_changed:
            line += f", changed: {', '.join(self.tables_changed)}"
        if self.tables_removed:
            line += f", {self.tables_removed} removed"
        if self.errors:
            line += f", {len(self.errors)} errors"
        return line


class UnsupportedSourceError(NotImplementedError):
    pass


# Databases served by the generic SQL adapter, with the driver extra to install.
SQL_ADAPTER_KINDS = {
    SourceKind.MYSQL: "mysql",
    SourceKind.SQLSERVER: "sqlserver",
    SourceKind.ORACLE: "oracle",
    SourceKind.SNOWFLAKE: "snowflake",
}


def open_adapter(source: DataSource) -> SourceAdapter:
    url = resolve_connection_url(source.connection_ref)
    if source.kind is SourceKind.POSTGRES:
        from ai_data_engineer.ingestion.postgres import PostgresAdapter

        return PostgresAdapter(url, scan_settings(source))
    if source.kind in SQL_ADAPTER_KINDS:
        from ai_data_engineer.ingestion.sql import SqlAdapter

        try:
            return SqlAdapter(url, scan_settings(source))
        except ModuleNotFoundError as exc:  # the database driver isn't installed
            extra = SQL_ADAPTER_KINDS[source.kind]
            raise UnsupportedSourceError(
                f"the driver for {source.kind.value} is missing ({exc.name}); install it with "
                f'python -m pip install -e ".[{extra}]"'
            ) from exc
    raise UnsupportedSourceError(f"no adapter for {source.kind.value} yet")


def scan_source(
    session: Session,
    source: DataSource,
    *,
    observed_at: datetime | None = None,
    adapter_factory: AdapterFactory = open_adapter,
) -> ScanResult:
    """Scan ``source`` and record the results. ``observed_at`` overrides "now" (the lab
    passes simulated dates)."""
    now = observed_at or utcnow()
    started = time.monotonic()
    run = IngestionRun(
        tenant_id=source.tenant_id,
        data_source_id=source.id,
        status=RunStatus.RUNNING,
        started_at=now,
    )
    session.add(run)
    session.flush()
    result = ScanResult(run_id=run.id, status=RunStatus.RUNNING)

    try:
        with adapter_factory(source) as adapter:
            tables = adapter.introspect()
            asset_keys = _record_structure(session, run, tables, now, result)
            for table in tables:
                if not table.profilable:
                    continue
                try:
                    measurement = adapter.profile(table)
                except SQLAlchemyError as exc:
                    result.errors[table.ref] = _short(exc)
                    continue
                _store_profile(session, run, asset_keys[table.ref], measurement, now)
                result.tables_profiled += 1
    except SQLAlchemyError as exc:
        run.status = result.status = RunStatus.FAILED
        run.error_message = _short(exc)
    else:
        run.status = result.status = RunStatus.PARTIAL if result.errors else RunStatus.SUCCEEDED
        if result.errors:
            run.error_message = "; ".join(f"{t}: {e}" for t, e in result.errors.items())[
                :MAX_ERROR_LENGTH
            ]

    run.finished_at = now
    run.stats = {
        "tables_seen": result.tables_seen,
        "tables_profiled": result.tables_profiled,
        "tables_created": len(result.tables_created),
        "tables_changed": result.tables_changed,
        "tables_removed": result.tables_removed,
        "errors": len(result.errors),
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    session.flush()
    return result


def _record_structure(
    session: Session,
    run: IngestionRun,
    tables: list[DiscoveredTable],
    now: datetime,
    result: ScanResult,
) -> dict[str, uuid.UUID]:
    asset_keys: dict[str, uuid.UUID] = {}
    for table in tables:
        recorded = record_asset(session, run, table.observation, observed_at=now)
        asset_keys[table.ref] = recorded.asset_key
        if recorded.asset_created:
            result.tables_created.append(table.ref)
        elif recorded.changed:
            result.tables_changed.append(table.ref)
    result.tables_seen = len(tables)
    result.tables_removed = len(
        mark_missing_assets(session, run, asset_keys.values(), observed_at=now)
    )
    return asset_keys


def _store_profile(
    session: Session,
    run: IngestionRun,
    asset_key: uuid.UUID,
    measurement: TableMeasurement,
    now: datetime,
) -> None:
    session.add(
        AssetProfile(
            tenant_id=run.tenant_id,
            asset_key=asset_key,
            ingestion_run_id=run.id,
            measured_at=now,
            row_count=measurement.row_count,
            row_count_is_estimate=measurement.row_count_is_estimate,
            size_bytes=measurement.size_bytes,
            last_modified_at=measurement.last_modified_at,
            properties=measurement.properties,
        )
    )
    column_keys = {
        name: column_key
        for name, column_key in session.execute(
            select(AssetColumn.name, AssetColumn.column_key).where(
                AssetColumn.asset_key == asset_key, AssetColumn.deleted_at.is_(None)
            )
        ).tuples()
    }
    for column in measurement.columns:
        session.add(
            ColumnProfile(
                tenant_id=run.tenant_id,
                column_key=column_keys[column.name],
                ingestion_run_id=run.id,
                measured_at=now,
                row_count=column.row_count,
                null_count=column.null_count,
                null_rate=column.null_rate,
                distinct_count=column.distinct_count,
                distinct_is_approx=column.distinct_is_approx,
                min_repr=column.min_repr,
                max_repr=column.max_repr,
                mean=column.mean,
                stddev=column.stddev,
                avg_length=column.avg_length,
                sample_fraction=measurement.sample_fraction,
                top_values=column.top_values,
                extra=(
                    {"distinct_case_insensitive": column.distinct_case_insensitive}
                    if column.distinct_case_insensitive is not None
                    else {}
                ),
            )
        )


def _short(exc: BaseException) -> str:
    message = str(getattr(exc, "orig", None) or exc).strip().splitlines()[0]
    return message[:MAX_ERROR_LENGTH]
