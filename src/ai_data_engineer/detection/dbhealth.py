"""Database health (Stage 3 add-on, Postgres first): problems with the database itself
rather than the data: indexes nobody uses (they slow every write), tables full of dead
rows (vacuum isn't keeping up), slow queries, sessions stuck inside a transaction (they
hold locks and block vacuum), and ID sequences close to running out (inserts start to
fail).

Readings come from the adapter (``db_health``); each kind is a condition: its finding
resolves when the reading no longer shows it. Only readings that actually ran resolve
anything, so a missing extension or permission never closes a finding by mistake.
"""

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.detection.recording import (
    latest_run_id,
    record_finding,
    resolve_finding,
)
from ai_data_engineer.discovery.catalog import load_catalog
from ai_data_engineer.graph.models import (
    ACTIVE_FINDING_STATUSES,
    DataSource,
    Finding,
    FindingCategory,
    IngestionRun,
    Severity,
    finding_fingerprint,
    utcnow,
)
from ai_data_engineer.ingestion.base import HealthLimits
from ai_data_engineer.ingestion.scan import AdapterFactory, open_adapter

CHECK_PREFIX = "db_"


class DbHealthSettings(BaseModel):
    unused_index_min_bytes: int = 10 * 1024 * 1024  # smaller unused indexes cost little
    unused_index_min_stats_days: float = 7.0  # usage counters must cover at least a week
    bloat_min_dead_rows: int = 10_000
    bloat_min_dead_ratio: float = 0.2  # dead rows >= 20% of the table
    slow_query_min_mean_ms: float = 1_000.0
    slow_query_min_calls: int = 10  # one slow ad-hoc report isn't a pattern
    idle_transaction_min_seconds: float = 300.0
    sequence_max_used: float = 0.8  # warn at 80% of the sequence's range
    severity: dict[str, Severity] = Field(
        default_factory=lambda: {
            "unused_index": Severity.LOW,
            "table_bloat": Severity.MEDIUM,
            "slow_query": Severity.MEDIUM,
            "idle_in_transaction": Severity.MEDIUM,
            "sequence_exhaustion": Severity.HIGH,
        }
    )

    def limits(self) -> HealthLimits:
        return HealthLimits(
            unused_index_min_bytes=self.unused_index_min_bytes,
            unused_index_min_stats_days=self.unused_index_min_stats_days,
            bloat_min_dead_rows=self.bloat_min_dead_rows,
            bloat_min_dead_ratio=self.bloat_min_dead_ratio,
            slow_query_min_mean_ms=self.slow_query_min_mean_ms,
            slow_query_min_calls=self.slow_query_min_calls,
            idle_transaction_min_seconds=self.idle_transaction_min_seconds,
            sequence_max_used=self.sequence_max_used,
        )


DEFAULT_DB_HEALTH_SETTINGS = DbHealthSettings()


@dataclass
class DbHealthResult:
    opened: int = 0
    refreshed: int = 0
    resolved: int = 0
    checks_run: tuple[str, ...] = ()
    skipped: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        if not self.checks_run:
            return "no database-health readings for this database type yet"
        text = (
            f"{len(self.checks_run)} readings: +{self.opened} new, {self.refreshed} still "
            f"there, -{self.resolved} resolved"
        )
        return text + (f"; skipped: {', '.join(self.skipped)}" if self.skipped else "")


def check_db_health(
    session: Session,
    source: DataSource,
    *,
    adapter_factory: AdapterFactory = open_adapter,
    now: datetime | None = None,
    settings: DbHealthSettings = DEFAULT_DB_HEALTH_SETTINGS,
) -> DbHealthResult:
    now = now or session.scalar(
        select(IngestionRun.started_at)
        .where(IngestionRun.data_source_id == source.id)
        .order_by(IngestionRun.started_at.desc())
        .limit(1)
    ) or utcnow()
    with adapter_factory(source) as adapter:
        reading = adapter.db_health(settings.limits())
    result = DbHealthResult(checks_run=reading.checks_run, skipped=reading.skipped)
    if not reading.checks_run:
        return result
    tables = {t.ref: t for t in load_catalog(session, source).tables.values()}
    run_id = latest_run_id(session, source)
    seen: set[str] = set()
    for issue in reading.issues:
        fp = finding_fingerprint(CHECK_PREFIX + issue.kind, source.id, issue.subject)
        seen.add(fp)
        table = tables.get(issue.table_ref) if issue.table_ref else None
        _, created = record_finding(
            session,
            source=source,
            fingerprint=fp,
            category=FindingCategory.DB_HEALTH,
            check_name=CHECK_PREFIX + issue.kind,
            severity=settings.severity.get(issue.kind, Severity.LOW),
            title=issue.summary[0].upper() + issue.summary[1:],
            description=issue.summary + ".",
            evidence={"kind": issue.kind, "subject": issue.subject, **issue.numbers},
            now=now,
            run_id=run_id,
            asset_key=table.asset_key if table else None,
        )
        if created:
            result.opened += 1
        else:
            result.refreshed += 1
    # Resolve what the readings that ran no longer show.
    checks = [CHECK_PREFIX + kind for kind in reading.checks_run]
    open_ones = session.scalars(
        select(Finding).where(
            Finding.data_source_id == source.id,
            Finding.check_name.in_(checks),
            Finding.status.in_(ACTIVE_FINDING_STATUSES),
        )
    ).all()
    for finding in open_ones:
        if finding.fingerprint not in seen and resolve_finding(
            session, source.tenant_id, finding.fingerprint, now
        ):
            result.resolved += 1
    return result
