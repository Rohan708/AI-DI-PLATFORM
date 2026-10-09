"""Row-level outliers (Stage 2.5): individual rows with a value far above everything else
in their column, e.g. an order line with quantity 500 where 1-3 is normal, or an order
100x the usual total. Computed inside the customer's database (``row_outliers`` on the
adapter); only counts, the median, the cutoff and the primary keys of a few offending
rows come back.

Which columns: numbers that measure something (amounts, quantities, prices). Keys and
codes are skipped: primary/unique keys, relationship columns, integer columns whose
every value is distinct, and names ending in id/no/code/... unless they name a measure.

A condition check: the finding resolves when no row is that extreme any more. No AI.
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ai_data_engineer.detection.recording import latest_run_id, record_finding, resolve_finding
from ai_data_engineer.detection.settings import DEFAULT_DETECTION_SETTINGS, DetectionSettings
from ai_data_engineer.discovery.catalog import Catalog, ColumnInfo, TableInfo, load_catalog
from ai_data_engineer.discovery.naming import KEY_WORDS, MEASURE_WORDS, tokens
from ai_data_engineer.graph.models import (
    DataSource,
    FindingCategory,
    IngestionRun,
    TypeFamily,
    finding_fingerprint,
    utcnow,
)
from ai_data_engineer.ingestion.base import KeyRef
from ai_data_engineer.ingestion.scan import AdapterFactory, open_adapter
from ai_data_engineer.rules.store import known_links

ROW_OUTLIER = "row_outlier"
NUMERIC = (TypeFamily.INTEGER, TypeFamily.DECIMAL, TypeFamily.FLOAT)


@dataclass
class OutlierCheckResult:
    columns: int = 0
    opened: int = 0
    refreshed: int = 0
    resolved: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        text = (
            f"{self.columns} columns checked: +{self.opened} new, {self.refreshed} still "
            f"unusual, -{self.resolved} resolved"
        )
        return text + (f", {len(self.errors)} could not run" if self.errors else "")


def is_measure(column: ColumnInfo, key_columns: set[str]) -> bool:
    """A number that measures something, rather than an identifier or a code."""
    if column.family not in NUMERIC or column.name in key_columns:
        return False
    words = tokens(column.name)  # abbreviations expanded: LINE_NO -> ["line", "number"]
    if words and words[-1] in KEY_WORDS and not set(words) & MEASURE_WORDS:
        return False
    return not (column.family is TypeFamily.INTEGER and column.effectively_unique)


def candidate_columns(
    catalog: Catalog, links: list[tuple[str, tuple[str, ...], str, tuple[str, ...]]]
) -> list[tuple[TableInfo, ColumnInfo]]:
    in_links: dict[str, set[str]] = {}
    for child, child_cols, parent, parent_cols in links:
        in_links.setdefault(child, set()).update(child_cols)
        in_links.setdefault(parent, set()).update(parent_cols)
    out = []
    for table in sorted(catalog.tables.values(), key=lambda t: t.ref):
        if table.kind != "table":
            continue
        keys = set(table.primary_key) | in_links.get(table.ref, set())
        keys |= {c for key in table.unique_keys for c in key}
        keys |= {c for fk in table.foreign_keys for c in fk.get("columns", [])}
        out += [(table, c) for c in table.columns.values() if is_measure(c, keys)]
    return out


def check_row_outliers(
    session: Session,
    source: DataSource,
    *,
    adapter_factory: AdapterFactory = open_adapter,
    now: datetime | None = None,
    settings: DetectionSettings = DEFAULT_DETECTION_SETTINGS,
) -> OutlierCheckResult:
    """``aide run``'s ``rows`` step. Stamped with the latest scan's time, like discovery."""
    now = now or session.scalar(
        select(IngestionRun.started_at)
        .where(IngestionRun.data_source_id == source.id)
        .order_by(IngestionRun.started_at.desc())
        .limit(1)
    ) or utcnow()
    catalog = load_catalog(session, source)
    columns = [
        (t, c)
        for t, c in candidate_columns(catalog, known_links(session, catalog))
        if (t.estimated_rows or 0) >= settings.outlier_min_rows
    ][: settings.outlier_max_columns]
    result = OutlierCheckResult()
    if not columns:
        return result
    run_id = latest_run_id(session, source)
    with adapter_factory(source) as adapter:
        for table, column in columns:
            ref = f"{table.ref}.{column.name}"
            try:
                found = adapter.row_outliers(
                    KeyRef(table.schema_name, table.name, (column.name,), table.estimated_rows),
                    table.primary_key,
                    sigmas=settings.outlier_sigmas,
                    min_ratio=settings.outlier_min_ratio,
                    min_spread=settings.outlier_min_spread,
                    sample_size=settings.outlier_sample_size,
                )
            except DBAPIError as exc:
                result.errors[ref] = str(exc.orig or exc).splitlines()[0][:300]
                continue
            result.columns += 1
            fingerprint = finding_fingerprint(ROW_OUTLIER, column.column_key)
            if not found.outlier_rows or found.median is None or found.cutoff is None:
                if resolve_finding(session, source.tenant_id, fingerprint, now):
                    result.resolved += 1
                continue
            ratio = found.max_ratio or 0.0
            sampled = " (on a sample of the table)" if found.sampled else ""
            _, created = record_finding(
                session,
                source=source,
                fingerprint=fingerprint,
                category=FindingCategory.ROW_OUTLIER,
                check_name=ROW_OUTLIER,
                severity=settings.severity_row_outlier,
                title=(
                    f"{found.outlier_rows:,} rows in {ref} are far above normal "
                    f"(up to {ratio:,.0f}x the typical {found.median:,.4g})"
                ),
                description=(
                    f"{found.outlier_rows:,} of {found.checked_rows:,} rows{sampled} have "
                    f"{column.name} above {found.cutoff:,.4g}: at least "
                    f"{settings.outlier_min_ratio:g}x the typical value {found.median:,.4g} and "
                    f"more than {settings.outlier_sigmas:g} typical spreads above it (on a log "
                    "scale). Often a typo (extra zeros), a unit mix-up or a test record."
                ),
                evidence={
                    "outlier_rows": found.outlier_rows,
                    "checked_rows": found.checked_rows,
                    "median": found.median,
                    "cutoff": found.cutoff,
                    "max_ratio": round(ratio, 2),
                    "threshold_sigmas": settings.outlier_sigmas,
                    "threshold_min_ratio": settings.outlier_min_ratio,
                    "sampled": found.sampled,
                    "sample_rows": found.sample,
                    "sample_identifies_by": list(table.primary_key),
                },
                now=now,
                run_id=run_id,
                asset_key=table.asset_key,
                column_key=column.column_key,
            )
            if created:
                result.opened += 1
            else:
                result.refreshed += 1
    return result
