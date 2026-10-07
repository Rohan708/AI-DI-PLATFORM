"""Writing findings with de-duplication.

The same problem seen again updates the existing *active* finding (``last_detected_at``,
evidence, numbers in the title) instead of creating a duplicate; when a check no longer
sees it, the finding is resolved. The full lifecycle (alerts, reopen rules, quiet
baseline) comes in Stage 1.6.
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
    FindingStatus,
    IngestionRun,
    Origin,
    Severity,
)


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
    """Insert a new finding or refresh the active one. Returns (finding, created)."""
    existing = active_finding(session, source.tenant_id, fingerprint)
    if existing is not None:
        existing.last_detected_at = now
        existing.last_run_id = run_id
        existing.severity = severity
        existing.title = title
        existing.description = description
        existing.evidence = evidence
        existing.confidence = confidence
        session.flush()
        return existing, False

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
    return finding, True


def resolve_finding(
    session: Session, tenant_id: uuid.UUID, fingerprint: str, now: datetime
) -> Finding | None:
    """Mark the active finding with this fingerprint resolved (the problem is gone)."""
    finding = active_finding(session, tenant_id, fingerprint)
    if finding is not None:
        finding.status = FindingStatus.RESOLVED
        finding.resolved_at = now
        session.flush()
    return finding
