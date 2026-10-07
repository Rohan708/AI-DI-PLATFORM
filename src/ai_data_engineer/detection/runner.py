"""Run every detection check for a source and record the findings.

- New problems become findings; problems seen again refresh their finding (no duplicates).
- Condition findings whose subject was re-evaluated and is fine again are resolved.
- Event findings (something happened) stay open until a person resolves them.

The caller owns the transaction.
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.detection.checks import ALL_CHECKS
from ai_data_engineer.detection.context import build_context
from ai_data_engineer.detection.framework import Check
from ai_data_engineer.detection.recording import record_finding, resolve_finding
from ai_data_engineer.detection.settings import DEFAULT_DETECTION_SETTINGS, DetectionSettings
from ai_data_engineer.graph.models import ACTIVE_FINDING_STATUSES, DataSource, Finding


@dataclass
class CheckSummary:
    opened: int = 0
    refreshed: int = 0
    resolved: int = 0
    learning: int = 0


@dataclass
class DetectionResult:
    scans_available: int = 0
    by_check: dict[str, CheckSummary] = field(default_factory=dict)

    @property
    def opened(self) -> int:
        return sum(s.opened for s in self.by_check.values())

    @property
    def refreshed(self) -> int:
        return sum(s.refreshed for s in self.by_check.values())

    @property
    def resolved(self) -> int:
        return sum(s.resolved for s in self.by_check.values())

    @property
    def learning(self) -> int:
        return sum(s.learning for s in self.by_check.values())

    def summary(self) -> str:
        line = f"{self.opened} new findings, {self.refreshed} still open, {self.resolved} resolved"
        if self.learning:
            line += f"; {self.learning} measurements still learning (need more scans)"
        return line


def detect(
    session: Session,
    source: DataSource,
    *,
    now: datetime | None = None,
    settings: DetectionSettings = DEFAULT_DETECTION_SETTINGS,
    checks: list[Check] = ALL_CHECKS,
) -> DetectionResult:
    ctx = build_context(session, source, settings, now)
    runs = {p.ingestion_run_id for ps in ctx.asset_profiles.values() for p in ps}
    result = DetectionResult(scans_available=len(runs))
    for check in checks:
        output = check.run(ctx)
        summary = result.by_check.setdefault(check.name, CheckSummary(learning=output.learning))
        seen: set[str] = set()
        for obs in output.observations:
            seen.add(obs.fingerprint)
            _, created = record_finding(
                session,
                source=source,
                fingerprint=obs.fingerprint,
                category=obs.category,
                check_name=check.name,
                severity=obs.severity,
                title=obs.title,
                description=obs.description,
                evidence=obs.evidence,
                now=ctx.now,
                run_id=ctx.current_run_id,
                asset_key=obs.asset_key,
                column_key=obs.column_key,
                relationship_id=obs.relationship_id,
            )
            if created:
                summary.opened += 1
            else:
                summary.refreshed += 1
        if check.kind == "condition":
            for fp in output.evaluated - seen:
                if resolve_finding(session, source.tenant_id, fp, ctx.now):
                    summary.resolved += 1
    return result


def open_findings(session: Session, source: DataSource) -> list[Finding]:
    return list(
        session.scalars(
            select(Finding)
            .where(
                Finding.data_source_id == source.id,
                Finding.status.in_(ACTIVE_FINDING_STATUSES),
            )
            .order_by(Finding.severity, Finding.check_name, Finding.title)
        )
    )
