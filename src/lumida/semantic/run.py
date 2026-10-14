"""Running the personal-data check on a source (the ``semantic`` step of ``lumida run``).

For every table's text columns the adapter counts, inside the database and on a bounded
sample, how many values look like each kind of personal data. From the counts:

- **the PII map**: columns that hold a kind (most of their values), stored on the
  column's latest profile (``extra["pii"]``) and shown by ``lumida pii``;
- **findings** for personal data where the column's name doesn't announce it (emails
  typed into a name field, phone numbers in free-text notes). A condition: resolved when
  the values are gone.

No value leaves the database; findings quote counts and shares only.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from lumida.detection.recording import latest_run_id, record_finding, resolve_finding
from lumida.discovery.catalog import ColumnInfo, TableInfo, load_catalog
from lumida.graph.models import (
    ColumnProfile,
    DataSource,
    FindingCategory,
    IngestionRun,
    TypeFamily,
    finding_fingerprint,
    utcnow,
)
from lumida.ingestion.base import KeyRef
from lumida.ingestion.scan import AdapterFactory, open_adapter
from lumida.ingestion.sql.pii import RegexNotSupportedError
from lumida.semantic.classify import ColumnPii, classify, holds, unexpected
from lumida.semantic.patterns import KINDS_BY_NAME, PII_KINDS
from lumida.semantic.settings import DEFAULT_SEMANTIC_SETTINGS, SemanticSettings

PII_UNEXPECTED = "pii_unexpected"
PATTERNS = {k.name: k.pattern for k in PII_KINDS}


@dataclass
class SemanticResult:
    tables: int = 0
    columns: int = 0
    mapped: list[ColumnPii] = field(default_factory=list)  # columns that hold a kind
    opened: int = 0
    refreshed: int = 0
    resolved: int = 0
    skipped: str | None = None  # why the check couldn't run on this database
    errors: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        if self.skipped:
            return f"personal-data check skipped: {self.skipped}"
        text = (
            f"{self.columns} text columns in {self.tables} tables: {len(self.mapped)} hold "
            f"personal data; +{self.opened} new, {self.refreshed} still there, "
            f"-{self.resolved} resolved"
        )
        return text + (f"; {len(self.errors)} tables could not be read" if self.errors else "")


def check_semantic(
    session: Session,
    source: DataSource,
    *,
    adapter_factory: AdapterFactory = open_adapter,
    now: datetime | None = None,
    settings: SemanticSettings = DEFAULT_SEMANTIC_SETTINGS,
) -> SemanticResult:
    now = (
        now
        or session.scalar(
            select(IngestionRun.started_at)
            .where(IngestionRun.data_source_id == source.id)
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        or utcnow()
    )
    catalog = load_catalog(session, source)
    run_id = latest_run_id(session, source)
    result = SemanticResult()
    targets = []
    for table in sorted(catalog.tables.values(), key=lambda t: t.ref):
        text_columns = [c for c in table.columns.values() if c.family is TypeFamily.STRING]
        if table.kind == "table" and text_columns:
            targets.append((table, text_columns[: settings.max_text_columns_per_table]))
    if not targets:
        return result

    with adapter_factory(source) as adapter:
        for table, columns in targets:
            names = tuple(c.name for c in columns)
            key = KeyRef(table.schema_name, table.name, names, table.estimated_rows)
            try:
                counts = adapter.scan_pii(key, PATTERNS, settings.sample_rows)
            except RegexNotSupportedError as exc:
                result.skipped = str(exc)
                return result
            except DBAPIError as exc:
                result.errors[table.ref] = str(exc.orig or exc).splitlines()[0][:300]
                continue
            result.tables += 1
            result.columns += len(columns)
            found = classify(table.ref, counts)
            mapped = holds(found, settings)
            result.mapped += mapped
            _remember_map(session, run_id, columns_by_name(columns), mapped)
            hits = {(f.column, f.kind): f for f in unexpected(found, settings)}
            for column in columns:
                for kind in PATTERNS:
                    fp = finding_fingerprint(PII_UNEXPECTED, column.column_key, kind)
                    hit = hits.get((column.name, kind))
                    if hit is not None:
                        _record(
                            session, source, table, column.column_key, hit, settings, now,
                            run_id, fp, result,
                        )  # fmt: skip
                    elif resolve_finding(session, source.tenant_id, fp, now):
                        result.resolved += 1
    return result


def columns_by_name(columns: list[ColumnInfo]) -> dict[str, uuid.UUID]:
    return {c.name: c.column_key for c in columns}


def _remember_map(
    session: Session,
    run_id: uuid.UUID | None,
    keys: dict[str, uuid.UUID],
    mapped: list[ColumnPii],
) -> None:
    """Note on each column's latest profile which kinds it holds (for docs and the UI)."""
    if run_id is None:
        return
    for f in mapped:
        profile = session.scalar(
            select(ColumnProfile).where(
                ColumnProfile.column_key == keys[f.column],
                ColumnProfile.ingestion_run_id == run_id,
            )
        )
        if profile is not None:
            pii = {**profile.extra.get("pii", {}), f.kind: round(f.share, 4)}
            profile.extra = {**profile.extra, "pii": pii}
    session.flush()


def _record(
    session: Session,
    source: DataSource,
    table: TableInfo,
    column_key: uuid.UUID,
    f: ColumnPii,
    settings: SemanticSettings,
    now: datetime,
    run_id: uuid.UUID | None,
    fingerprint: str,
    result: SemanticResult,
) -> None:
    kind = KINDS_BY_NAME[f.kind]
    where = f"{f.table}.{f.column}"
    _, created = record_finding(
        session,
        source=source,
        fingerprint=fingerprint,
        category=FindingCategory.SEMANTIC,
        check_name=PII_UNEXPECTED,
        severity=settings.severity_sensitive if kind.sensitive else settings.severity,
        title=(
            f"{f.matching:,} values in {where} look like {kind.label} "
            f"({f.share:.1%} of {f.checked:,} checked)"
        ),
        description=(
            f"{f.matching:,} of {f.checked:,} values checked in {where} look like "
            f"{kind.label}, but the column's name doesn't say it holds them. Personal data "
            "outside the columns meant for it is easy to leak (exports, logs, analytics) and "
            "is missed by access rules and deletion requests. Counted inside the database; "
            "no value was read out."
        ),
        evidence={
            "kind": f.kind,
            "matching_values": f.matching,
            "checked_values": f.checked,
            "share": round(f.share, 4),
            "sensitive": kind.sensitive,
            "sample_rows_limit": settings.sample_rows,
        },
        now=now,
        run_id=run_id,
        asset_key=table.asset_key,
        column_key=column_key,
    )
    if created:
        result.opened += 1
    else:
        result.refreshed += 1
