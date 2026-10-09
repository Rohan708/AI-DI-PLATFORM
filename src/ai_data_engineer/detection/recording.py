"""Writing findings: de-duplication, human decisions, and the audit trail.

- The same problem seen again refreshes the existing *active* finding (``last_detected_at``,
  evidence, numbers in the title) instead of creating a duplicate.
- A finding a person **rejected** ("this is normal here") is never raised again for the
  same subject: the rejected finding only notes that it was seen again.
- When a check no longer sees a condition, its finding is resolved by ``aide``.
- Every status change is recorded as a ``FindingEvent`` (who, when, from -> to, note).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.graph.models import (
    ACTIVE_FINDING_STATUSES,
    DataSource,
    Finding,
    FindingCategory,
    FindingEvent,
    FindingStatus,
    IngestionRun,
    Origin,
    Severity,
)

SYSTEM_ACTOR = "aide"
# Evidence written by others than the check (AI explanations), kept when a check refreshes.
KEPT_EVIDENCE_KEYS = ("explanation",)

# Which status changes a person may make (system changes use the same rules).
ALLOWED_TRANSITIONS: dict[FindingStatus, set[FindingStatus]] = {
    FindingStatus.OPEN: {FindingStatus.CONFIRMED, FindingStatus.REJECTED, FindingStatus.RESOLVED},
    FindingStatus.CONFIRMED: {FindingStatus.RESOLVED, FindingStatus.REJECTED},
    FindingStatus.REJECTED: {FindingStatus.OPEN},  # "un-reject"
    FindingStatus.RESOLVED: {FindingStatus.OPEN},  # reopen
}


class InvalidTransitionError(ValueError):
    pass


def latest_run_id(session: Session, source: DataSource) -> uuid.UUID | None:
    return session.scalar(
        select(IngestionRun.id)
        .where(IngestionRun.data_source_id == source.id)
        .order_by(IngestionRun.started_at.desc())
        .limit(1)
    )


def active_finding(session: Session, tenant_id: uuid.UUID, fingerprint: str) -> Finding | None:
    return session.scalar(
        select(Finding).where(
            Finding.tenant_id == tenant_id,
            Finding.fingerprint == fingerprint,
            Finding.status.in_(ACTIVE_FINDING_STATUSES),
        )
    )


def rejected_finding(session: Session, tenant_id: uuid.UUID, fingerprint: str) -> Finding | None:
    return session.scalar(
        select(Finding)
        .where(
            Finding.tenant_id == tenant_id,
            Finding.fingerprint == fingerprint,
            Finding.status == FindingStatus.REJECTED,
        )
        .order_by(Finding.last_detected_at.desc())
        .limit(1)
    )


def change_status(
    session: Session,
    finding: Finding,
    to_status: FindingStatus,
    *,
    actor: str,
    now: datetime,
    note: str | None = None,
) -> FindingEvent:
    """Move a finding to a new status and record who did it and why."""
    if to_status not in ALLOWED_TRANSITIONS[finding.status]:
        raise InvalidTransitionError(
            f"a finding that is {finding.status.value} can't become {to_status.value}"
        )
    event = FindingEvent(
        tenant_id=finding.tenant_id,
        finding_id=finding.id,
        at=now,
        actor=actor,
        from_status=finding.status,
        to_status=to_status,
        note=note,
    )
    finding.status = to_status
    if to_status is FindingStatus.RESOLVED:
        finding.resolved_at = now
    elif to_status is FindingStatus.OPEN:
        finding.resolved_at = None
    if actor != SYSTEM_ACTOR:
        finding.reviewed_by, finding.reviewed_at, finding.review_note = actor, now, note
    session.add(event)
    session.flush()
    return event


def record_finding(
    session: Session,
    *,
    source: DataSource,
    fingerprint: str,
    category: FindingCategory,
    check_name: str,
    severity: Severity,
    title: str,
    description: str,
    evidence: dict[str, Any],
    now: datetime,
    run_id: uuid.UUID | None = None,
    asset_key: uuid.UUID | None = None,
    column_key: uuid.UUID | None = None,
    relationship_id: uuid.UUID | None = None,
    rule_id: uuid.UUID | None = None,
    origin: Origin = Origin.SYSTEM,
    confidence: float | None = None,
) -> tuple[Finding, bool]:
    """Insert a new finding or refresh the active one. Returns (finding, created).

    If a person rejected this problem before, nothing new is opened: the rejected finding
    is returned (status ``rejected``, ``created`` False) with ``last_detected_at`` updated.
    """
    existing = active_finding(session, source.tenant_id, fingerprint)
    if existing is not None:
        existing.last_detected_at = now
        existing.last_run_id = run_id
        existing.severity = severity
        existing.title = title
        existing.description = description
        # Replaces any earlier "not re-checked" marker; an AI explanation is kept (it notes
        # which title it explained, so it can be shown as possibly out of date).
        kept = {k: existing.evidence[k] for k in KEPT_EVIDENCE_KEYS if k in existing.evidence}
        existing.evidence = {**evidence, **kept}
        existing.confidence = confidence
        session.flush()
        return existing, False

    rejected = rejected_finding(session, source.tenant_id, fingerprint)
    if rejected is not None:
        rejected.last_detected_at = now
        session.flush()
        return rejected, False

    finding = Finding(
        tenant_id=source.tenant_id,
        data_source_id=source.id,
        category=category,
        check_name=check_name,
        fingerprint=fingerprint,
        severity=severity,
        status=FindingStatus.OPEN,
        origin=origin,
        asset_key=asset_key,
        column_key=column_key,
        relationship_id=relationship_id,
        rule_id=rule_id,
        title=title,
        description=description,
        evidence=evidence,
        confidence=confidence,
        first_detected_at=now,
        last_detected_at=now,
        first_run_id=run_id,
        last_run_id=run_id,
    )
    session.add(finding)
    session.flush()
    session.add(
        FindingEvent(
            tenant_id=finding.tenant_id,
            finding_id=finding.id,
            at=now,
            actor=SYSTEM_ACTOR,
            from_status=None,
            to_status=FindingStatus.OPEN,
            note="detected",
        )
    )
    session.flush()
    return finding, True


def resolve_finding(
    session: Session,
    tenant_id: uuid.UUID,
    fingerprint: str,
    now: datetime,
    note: str = "no longer detected",
) -> Finding | None:
    """Resolve the active finding with this fingerprint (the problem is gone)."""
    finding = active_finding(session, tenant_id, fingerprint)
    if finding is not None:
        change_status(
            session, finding, FindingStatus.RESOLVED, actor=SYSTEM_ACTOR, now=now, note=note
        )
    return finding
