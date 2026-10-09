"""``aide run``: the whole nightly job for one source, in order:

    scan -> discover -> rules -> rows -> dbhealth -> detect -> reconcile -> health -> alert

Each step commits on its own, so a later failure never loses earlier work. If the scan
fails, the remaining steps are skipped (they would only re-judge yesterday's data). Every
step's outcome is reported; the exit code is 0 only if all steps succeeded.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ai_data_engineer.alerting.notifier import Notifier, notifier_for
from ai_data_engineer.alerting.router import send_alerts
from ai_data_engineer.detection.dbhealth import check_db_health
from ai_data_engineer.detection.rows import check_row_outliers
from ai_data_engineer.detection.runner import detect
from ai_data_engineer.discovery.discover import discover
from ai_data_engineer.graph.models import DataSource, RunStatus, utcnow
from ai_data_engineer.health_score.engine import compute_health
from ai_data_engineer.ingestion.scan import AdapterFactory, open_adapter, scan_source
from ai_data_engineer.reconcile.run import pairs_of, reconcile
from ai_data_engineer.rules.checks import check_rules

STEPS = (
    "scan", "discover", "rules", "rows", "dbhealth", "detect", "reconcile", "health", "alert",
)


@dataclass
class StepOutcome:
    step: str
    ok: bool
    detail: str


@dataclass
class PipelineResult:
    source: str
    steps: list[StepOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps) and len(self.steps) == len(STEPS)

    def summary(self) -> str:
        lines = [f"aide run {self.source}: {'ok' if self.ok else 'FAILED'}"]
        for s in self.steps:
            lines.append(f"  {'ok ' if s.ok else 'ERR'} {s.step:9} {s.detail}")
        skipped = STEPS[len(self.steps) :]
        if skipped:
            lines.append(f"  skipped: {', '.join(skipped)}")
        return "\n".join(lines)


def run_pipeline(
    session: Session,
    source: DataSource,
    *,
    observed_at: datetime | None = None,
    alert: bool = True,
    notifier: Notifier | None = None,
    adapter_factory: AdapterFactory = open_adapter,
) -> PipelineResult:
    result = PipelineResult(source.name)
    now = observed_at or utcnow()

    def step(name: str, action: Callable[[], str]) -> bool:
        try:
            detail = action()
        except Exception as exc:  # a nightly job must report, not crash
            session.rollback()
            result.steps.append(StepOutcome(name, False, f"{type(exc).__name__}: {exc}"[:300]))
            return False
        session.commit()
        result.steps.append(StepOutcome(name, True, detail))
        return True

    def scan() -> str:
        scanned = scan_source(session, source, observed_at=now, adapter_factory=adapter_factory)
        if scanned.status is RunStatus.FAILED:
            session.commit()  # keep the failed run on record
            raise RuntimeError(f"scan failed: {'; '.join(scanned.errors.values()) or 'see run'}")
        return scanned.summary()

    if not step("scan", scan):
        return result
    if not step(
        "discover", lambda: discover(session, source, adapter_factory=adapter_factory).summary()
    ):
        return result
    # Approved business rules: deterministic SQL, no AI (proposing rules is separate).
    if not step(
        "rules", lambda: check_rules(session, source, adapter_factory=adapter_factory).summary()
    ):
        return result
    # Row-level outliers, computed inside the customer database (Stage 2.5).
    if not step(
        "rows",
        lambda: check_row_outliers(session, source, adapter_factory=adapter_factory).summary(),
    ):
        return result
    if not step(
        "dbhealth",
        lambda: check_db_health(session, source, adapter_factory=adapter_factory).summary(),
    ):
        return result
    if not step("detect", lambda: detect(session, source).summary()):
        return result
    # Pairs this source belongs to (either side), from both sides' latest scans.
    if not step(
        "reconcile",
        lambda: "; ".join(reconcile(session, p).summary() for p in pairs_of(session, source))
        or "no reconciliation pairs",
    ):
        return result
    if not step("health", lambda: compute_health(session, source, now).summary().splitlines()[0]):
        return result
    if alert:
        step(
            "alert",
            lambda: send_alerts(session, source, notifier or notifier_for(source), now).summary(),
        )
    else:
        result.steps.append(StepOutcome("alert", True, "skipped (--no-alert)"))
    return result
