"""Decide what to alert, build one digest, send it, and remember what was sent.

Rules:
1. **Quiet baseline.** The first alerting run on a source sends nothing: every open
   finding is recorded as already "seen" (``alerted_at`` set) and the source's
   ``baseline_completed_at`` is stamped. A messy legacy database won't flood the channel.
2. **Only new problems.** A finding is alerted at most once (``alerted_at``). A problem that
   comes back after being resolved is a new finding and alerts again.
3. **Threshold.** Only findings at or above ``min_severity`` (default medium) are alerted;
   low ones (e.g. unindexed FKs) stay in ``aide findings`` and the docs.
4. **One digest per run**, not one message per finding: counts by severity plus the top
   items, most severe first, each with its numbers.
5. If sending fails, nothing is marked as alerted, so the next run tries again.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.alerting.notifier import AlertMessage, Notifier
from ai_data_engineer.graph.models import (
    ACTIVE_FINDING_STATUSES,
    DataSource,
    Finding,
    Severity,
)

SEVERITY_RANK = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


@dataclass(frozen=True)
class AlertSettings:
    min_severity: Severity = Severity.MEDIUM
    max_items: int = 15  # digest lines shown; the rest are summarised in the counts
    group_at: int = 3  # this many findings of one check+severity collapse into one line


DEFAULT_ALERT_SETTINGS = AlertSettings()


@dataclass
class AlertResult:
    quiet_baseline: bool = False
    baselined: int = 0  # findings recorded silently by the quiet baseline
    sent: int = 0  # findings included in the digest
    error: str | None = None

    def summary(self) -> str:
        if self.quiet_baseline:
            return f"quiet baseline: {self.baselined} existing findings recorded, not alerted"
        if self.error:
            return f"alert NOT sent ({self.error}); will retry next run"
        return f"alerted {self.sent} new findings" if self.sent else "nothing new to alert"


def send_alerts(
    session: Session,
    source: DataSource,
    notifier: Notifier,
    now: datetime,
    settings: AlertSettings = DEFAULT_ALERT_SETTINGS,
) -> AlertResult:
    unalerted = list(
        session.scalars(
            select(Finding).where(
                Finding.data_source_id == source.id,
                Finding.status.in_(ACTIVE_FINDING_STATUSES),
                Finding.alerted_at.is_(None),
            )
        )
    )
    if source.baseline_completed_at is None:
        for finding in unalerted:
            finding.alerted_at = now
        source.baseline_completed_at = now
        session.flush()
        return AlertResult(quiet_baseline=True, baselined=len(unalerted))

    threshold = SEVERITY_RANK[settings.min_severity]
    due = sorted(
        (f for f in unalerted if SEVERITY_RANK[f.severity] <= threshold),
        key=lambda f: (SEVERITY_RANK[f.severity], f.check_name, f.title),
    )
    if not due:
        return AlertResult()
    try:
        notifier.send(build_digest(source, due, settings))
    except OSError as exc:  # network errors, timeouts, HTTP errors (urllib raises OSError)
        return AlertResult(error=str(exc)[:200])
    for finding in due:
        finding.alerted_at = now
    session.flush()
    return AlertResult(sent=len(due))


def build_digest(
    source: DataSource, findings: list[Finding], settings: AlertSettings = DEFAULT_ALERT_SETTINGS
) -> AlertMessage:
    """Most severe first; ``group_at`` or more findings of the same check and severity
    become one line ("8 x volume_drop: ..."), so one incident spilling into many tables
    doesn't push everything else out of the message."""
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity.value] = counts.get(f.severity.value, 0) + 1
    by_severity = ", ".join(
        f"{n} {sev}"
        for sev, n in sorted(counts.items(), key=lambda kv: SEVERITY_RANK[Severity(kv[0])])
    )
    noun = "problem" if len(findings) == 1 else "problems"

    groups: dict[tuple[int, str], list[Finding]] = {}
    for f in sorted(findings, key=lambda f: (SEVERITY_RANK[f.severity], f.check_name, f.title)):
        groups.setdefault((SEVERITY_RANK[f.severity], f.check_name), []).append(f)
    all_lines: list[str] = []
    for (_, check), members in groups.items():
        severity = members[0].severity.value
        if len(members) >= settings.group_at:
            all_lines.append(
                f"[{severity}] {len(members)} x {check}: {members[0].title} "
                f"(and {len(members) - 1} more like it)"
            )
        else:
            all_lines.extend(f"[{severity}] {f.title}" for f in members)
    lines = all_lines[: settings.max_items]
    if len(all_lines) > settings.max_items:
        more = len(all_lines) - settings.max_items
        lines.append(f"... and {more} more (`aide findings {source.name}`)")
    return AlertMessage(
        title=f"AI Data Engineer: {len(findings)} new {noun} in {source.name} ({by_severity})",
        lines=lines,
    )
