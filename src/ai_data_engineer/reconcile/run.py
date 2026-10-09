"""Running a reconciliation pair: match tables, compare their latest measurements
(``compare.py``), and keep one finding per difference (resolved when it's gone).

No extra queries against either database: both sides were measured by their own scans,
so this works across any two adapters (Postgres app DB vs Snowflake warehouse, ...).
Findings belong to the left source; the right side is named in the evidence.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ai_data_engineer.detection.recording import record_finding, resolve_finding
from ai_data_engineer.discovery.catalog import Catalog, load_catalog
from ai_data_engineer.graph.models import (
    AssetProfile,
    ColumnProfile,
    DataSource,
    FindingCategory,
    ReconciliationPair,
    Severity,
    finding_fingerprint,
    utcnow,
)
from ai_data_engineer.graph.queries import get_asset_profile_history, get_column_profile_history
from ai_data_engineer.reconcile.compare import ColumnSnap, Difference, TableSnap, compare
from ai_data_engineer.reconcile.settings import ReconcileSettings

CHECK_PREFIX = "reconcile_"
SEVERITY = {"row_count": Severity.HIGH, "table_missing": Severity.HIGH}  # others: medium


class PairNotFoundError(LookupError):
    pass


class PairConfigError(ValueError):
    pass


@dataclass
class ReconcileResult:
    pair: str
    tables_compared: int = 0
    unmatched: list[str] = field(default_factory=list)  # left tables with no copy found
    too_far_apart: list[str] = field(default_factory=list)  # scans too far apart to compare
    opened: int = 0
    refreshed: int = 0
    resolved: int = 0

    def summary(self) -> str:
        text = (
            f"{self.pair}: {self.tables_compared} tables compared: +{self.opened} new, "
            f"{self.refreshed} still different, -{self.resolved} resolved"
        )
        if self.unmatched:
            text += f"; {len(self.unmatched)} without a copy"
        if self.too_far_apart:
            text += f"; {len(self.too_far_apart)} not compared (scans too far apart)"
        return text


def add_pair(
    session: Session, name: str, left: DataSource, right: DataSource, settings: dict[str, object]
) -> ReconciliationPair:
    if left.id == right.id:
        raise PairConfigError("a source can't be reconciled with itself")
    if session.scalar(select(ReconciliationPair.id).where(ReconciliationPair.name == name)):
        raise PairConfigError(f"a reconciliation pair named {name!r} already exists")
    validated = ReconcileSettings.model_validate(settings)
    pair = ReconciliationPair(
        tenant_id=left.tenant_id,
        name=name,
        left_source_id=left.id,
        right_source_id=right.id,
        settings=validated.model_dump(mode="json", exclude_defaults=True),
    )
    session.add(pair)
    session.flush()
    return pair


def get_pair(session: Session, name: str) -> ReconciliationPair:
    pair = session.scalar(select(ReconciliationPair).where(ReconciliationPair.name == name))
    if pair is None:
        raise PairNotFoundError(f"no reconciliation pair named {name!r}")
    return pair


def pairs_of(session: Session, source: DataSource) -> list[ReconciliationPair]:
    return list(
        session.scalars(
            select(ReconciliationPair)
            .where(
                or_(
                    ReconciliationPair.left_source_id == source.id,
                    ReconciliationPair.right_source_id == source.id,
                )
            )
            .order_by(ReconciliationPair.name)
        )
    )


def reconcile(
    session: Session, pair: ReconciliationPair, *, now: datetime | None = None
) -> ReconcileResult:
    settings = ReconcileSettings.model_validate(pair.settings)
    left_source = session.get_one(DataSource, pair.left_source_id)
    right_source = session.get_one(DataSource, pair.right_source_id)
    left, right = load_catalog(session, left_source), load_catalog(session, right_source)
    now = now or utcnow()
    result = ReconcileResult(pair.name)

    for left_ref, right_ref in table_pairs(left, right, settings, result):
        lt, rt = snapshot(session, left, left_ref), snapshot(session, right, right_ref)
        if lt.measured_at and rt.measured_at:
            gap_hours = abs((lt.measured_at - rt.measured_at).total_seconds()) / 3600
            if gap_hours > settings.max_scan_gap_hours:
                result.too_far_apart.append(left_ref)
                continue
        result.tables_compared += 1
        comparison = compare(lt, rt, settings)
        flagged = set()
        for diff in comparison.differences:
            flagged.add((diff.metric, diff.column))
            skipped = comparison.columns_skipped
            _record(session, pair, left_source, lt, rt, diff, skipped, now, result)
        for metric, column in comparison.checked:
            if (metric, column) not in flagged:
                fp = _fingerprint(pair.id, lt.asset_key, metric, column)
                if resolve_finding(session, left_source.tenant_id, fp, now):
                    result.resolved += 1
    _missing_tables(session, pair, left_source, left, settings, result, now)
    return result


def table_pairs(
    left: Catalog, right: Catalog, settings: ReconcileSettings, result: ReconcileResult
) -> list[tuple[str, str]]:
    """(left ref, right ref) for every left table with a copy on the right."""
    pairs = []
    by_lower = {ref.lower(): ref for ref in right.tables}
    explicit = settings.tables
    for ref, table in sorted(left.tables.items()):
        if explicit:
            if ref not in explicit:
                continue
            wanted = explicit[ref]
        else:
            if table.kind != "table":
                continue
            wanted = f"{settings.schema_map.get(table.schema_name, table.schema_name)}.{table.name}"
        found = wanted if wanted in right.tables else by_lower.get(wanted.lower())
        if found is None:
            result.unmatched.append(ref)
            continue
        pairs.append((ref, found))
    return pairs


def snapshot(session: Session, catalog: Catalog, ref: str) -> TableSnap:
    table = catalog.tables[ref]
    latest: list[AssetProfile] = get_asset_profile_history(session, table.asset_key, limit=1)
    columns = {}
    for column in table.columns.values():
        profiles: list[ColumnProfile] = get_column_profile_history(
            session, column.column_key, limit=1
        )
        p = profiles[0] if profiles else None
        columns[column.name.lower()] = ColumnSnap(
            name=column.name,
            family=column.family,
            column_key=column.column_key,
            row_count=p.row_count if p else None,
            null_count=p.null_count if p else None,
            distinct_count=p.distinct_count if p else None,
            distinct_is_approx=p.distinct_is_approx if p else False,
            min_repr=p.min_repr if p else None,
            max_repr=p.max_repr if p else None,
            mean=p.mean if p else None,
        )
    a = latest[0] if latest else None
    return TableSnap(
        ref=ref,
        asset_key=table.asset_key,
        rows=a.row_count if a else None,
        rows_estimated=a.row_count_is_estimate if a else False,
        measured_at=a.measured_at if a else None,
        columns=columns,
    )


def _fingerprint(pair_id: uuid.UUID, asset_key: object, metric: str, column: str | None) -> str:
    return finding_fingerprint("reconcile", pair_id, asset_key, metric, (column or "").lower())


def _record(
    session: Session,
    pair: ReconciliationPair,
    source: DataSource,
    left: TableSnap,
    right: TableSnap,
    diff: Difference,
    columns_skipped: bool,
    now: datetime,
    result: ReconcileResult,
) -> None:
    column = left.columns.get((diff.column or "").lower())
    note = " Columns weren't compared because the row counts differ." if columns_skipped else ""
    _, created = record_finding(
        session,
        source=source,
        fingerprint=_fingerprint(pair.id, left.asset_key, diff.metric, diff.column),
        category=FindingCategory.CROSS_SYSTEM,
        check_name=CHECK_PREFIX + diff.metric,
        severity=SEVERITY.get(diff.metric, Severity.MEDIUM),
        title=f"[{pair.name}] {diff.detail}",
        description=(
            f"{left.ref} and its copy {right.ref} (pair {pair.name!r}) disagree: "
            f"{diff.detail}. Measured {_when(left)} and {_when(right)}.{note}"
        ),
        evidence={
            "pair": pair.name,
            "metric": diff.metric,
            "left_table": left.ref,
            "right_table": right.ref,
            "column": diff.column,
            "left_value": diff.left,
            "right_value": diff.right,
            "left_measured_at": left.measured_at.isoformat() if left.measured_at else None,
            "right_measured_at": right.measured_at.isoformat() if right.measured_at else None,
            "left_rows_estimated": left.rows_estimated,
            "right_rows_estimated": right.rows_estimated,
        },
        now=now,
        asset_key=left.asset_key,
        column_key=column.column_key if column else None,
    )
    if created:
        result.opened += 1
    else:
        result.refreshed += 1


def _missing_tables(
    session: Session,
    pair: ReconciliationPair,
    source: DataSource,
    left: Catalog,
    settings: ReconcileSettings,
    result: ReconcileResult,
    now: datetime,
) -> None:
    """With an explicit table list, a table without its copy is a finding."""
    for ref, table in left.tables.items():
        if ref not in settings.tables:
            continue
        fp = _fingerprint(pair.id, table.asset_key, "table_missing", None)
        if ref not in result.unmatched:
            if resolve_finding(session, source.tenant_id, fp, now):
                result.resolved += 1
            continue
        _, created = record_finding(
            session,
            source=source,
            fingerprint=fp,
            category=FindingCategory.CROSS_SYSTEM,
            check_name=CHECK_PREFIX + "table_missing",
            severity=Severity.HIGH,
            title=f"[{pair.name}] {settings.tables[ref]} (the copy of {ref}) was not found",
            description=f"Pair {pair.name!r} expects {ref} to be copied to "
            f"{settings.tables[ref]}, but the copy's latest scan has no such table.",
            evidence={"pair": pair.name, "left_table": ref, "right_table": settings.tables[ref]},
            now=now,
            asset_key=table.asset_key,
        )
        if created:
            result.opened += 1
        else:
            result.refreshed += 1


def _when(t: TableSnap) -> str:
    return t.measured_at.strftime("%Y-%m-%d %H:%M UTC") if t.measured_at else "never"
