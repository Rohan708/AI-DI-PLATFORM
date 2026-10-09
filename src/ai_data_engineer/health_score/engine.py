"""Compute and store health snapshots for a source; read the latest and the trend."""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.detection.recording import latest_run_id
from ai_data_engineer.discovery.catalog import load_catalog
from ai_data_engineer.graph.models import (
    ACTIVE_FINDING_STATUSES,
    DataSource,
    Finding,
    HealthScope,
    HealthSnapshot,
    IngestionRun,
    Severity,
    utcnow,
)
from ai_data_engineer.health_score.score import Score, group_score, table_score


@dataclass
class TableHealth:
    ref: str
    asset_key: uuid.UUID
    score: Score


@dataclass
class HealthReport:
    computed_at: datetime
    source: Score
    previous_source_score: float | None
    schemas: dict[str, Score] = field(default_factory=dict)
    tables: list[TableHealth] = field(default_factory=list)  # worst first

    @property
    def change(self) -> float | None:
        if self.previous_source_score is None:
            return None
        return round(self.source.score - self.previous_source_score, 1)

    def summary(self, top: int = 5) -> str:
        trend = "" if self.change is None else f" ({self.change:+.1f} since last time)"
        worst = self.source.min_child
        lines = [
            f"health {self.source.score:.1f}/100{trend}; worst table {worst:.0f}"
            if worst is not None
            else f"health {self.source.score:.1f}/100{trend}"
        ]
        for schema, s in sorted(self.schemas.items()):
            worst_in_schema = s.min_child or 0
            lines.append(
                f"  schema {schema:12} {s.score:5.1f}  (worst table {worst_in_schema:.0f})"
            )
        unhealthy = [t for t in self.tables if t.score.score < 100][:top]
        for t in unhealthy:
            counts = ", ".join(f"{n} {sev}" for sev, n in t.score.open_findings.items())
            lines.append(f"  {t.score.score:5.1f}  {t.ref}  ({counts})")
        return "\n".join(lines)


def compute_health(
    session: Session, source: DataSource, now: datetime | None = None
) -> HealthReport:
    """Score every current table from its open findings, roll up to schemas and the whole
    source, and store one snapshot per table, schema and source. ``now`` defaults to the
    latest scan's time (keeps the lab's simulated calendar consistent)."""
    now = now or _latest_scan_time(session, source) or utcnow()
    previous = _previous_source_score(session, source)
    catalog = load_catalog(session, source)
    severities: dict[uuid.UUID, list[Severity]] = defaultdict(list)
    for asset_key, severity in session.execute(
        select(Finding.asset_key, Finding.severity).where(
            Finding.data_source_id == source.id,
            Finding.status.in_(ACTIVE_FINDING_STATUSES),
            Finding.asset_key.is_not(None),
        )
    ):
        if asset_key is not None:  # excluded by the query; narrows the type
            severities[asset_key].append(severity)

    run_id = latest_run_id(session, source)
    tables: list[TableHealth] = []
    by_schema: dict[str, list[Score]] = defaultdict(list)
    for table in catalog.tables.values():
        s = table_score(severities.get(table.asset_key, []))
        tables.append(TableHealth(table.ref, table.asset_key, s))
        by_schema[table.schema_name].append(s)
        _store(session, source, run_id, now, HealthScope.ASSET, s, asset_key=table.asset_key)
    schemas = {name: group_score(scores) for name, scores in by_schema.items()}
    for name, s in schemas.items():
        _store(session, source, run_id, now, HealthScope.SCHEMA, s, schema_name=name)
    overall = group_score([t.score for t in tables])
    _store(session, source, run_id, now, HealthScope.SOURCE, overall)
    session.flush()

    tables.sort(key=lambda t: (t.score.score, t.ref))
    return HealthReport(now, overall, previous, schemas, tables)


def _store(
    session: Session,
    source: DataSource,
    run_id: uuid.UUID | None,
    now: datetime,
    scope: HealthScope,
    s: Score,
    *,
    asset_key: uuid.UUID | None = None,
    schema_name: str | None = None,
) -> None:
    session.add(
        HealthSnapshot(
            tenant_id=source.tenant_id,
            data_source_id=source.id,
            ingestion_run_id=run_id,
            computed_at=now,
            scope=scope,
            asset_key=asset_key,
            schema_name=schema_name,
            score=s.score,
            min_child_score=s.min_child,
            open_findings=s.open_findings,
        )
    )


def _latest_scan_time(session: Session, source: DataSource) -> datetime | None:
    return session.scalar(
        select(IngestionRun.started_at)
        .where(IngestionRun.data_source_id == source.id)
        .order_by(IngestionRun.started_at.desc())
        .limit(1)
    )


def _previous_source_score(session: Session, source: DataSource) -> float | None:
    return session.scalar(
        select(HealthSnapshot.score)
        .where(
            HealthSnapshot.data_source_id == source.id,
            HealthSnapshot.scope == HealthScope.SOURCE,
        )
        .order_by(HealthSnapshot.computed_at.desc())
        .limit(1)
    )
