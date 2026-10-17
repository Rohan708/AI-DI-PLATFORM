"""Reading the metadata store for the API: rows to response shapes. No writes here."""

import uuid
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from lumida.api.schemas import (
    EventOut,
    FindingDetail,
    FindingOut,
    HealthOut,
    RelationshipOut,
    RuleOut,
    RunOut,
    ScoreOut,
    SourceOut,
    TableScoreOut,
)
from lumida.graph.models import (
    ACTIVE_FINDING_STATUSES,
    Asset,
    AssetColumn,
    DataSource,
    Finding,
    FindingEvent,
    HealthScope,
    HealthSnapshot,
    IngestionRun,
    Relationship,
    Rule,
)
from lumida.ingestion.sources import scan_settings


class NotFoundError(LookupError):
    pass


def owned[T: (Finding, Rule, Relationship)](
    session: Session, model: type[T], id_: uuid.UUID, tenant_id: uuid.UUID
) -> T:
    """A row by id, if it belongs to the caller's tenant (otherwise it doesn't exist for them)."""
    row = session.get(model, id_)
    if row is None or row.tenant_id != tenant_id:
        raise NotFoundError(f"no {model.__tablename__.replace('_', ' ')} with id {id_}")
    return row


def table_ref(session: Session, asset_key: uuid.UUID | None) -> str | None:
    if asset_key is None:
        return None
    asset = session.get_one(Asset, asset_key)
    return f"{asset.schema_name}.{asset.name}"


def column_name(session: Session, column_key: uuid.UUID | None) -> str | None:
    return None if column_key is None else session.get_one(AssetColumn, column_key).name


# --- sources -------------------------------------------------------------------------------


def run_out(run: IngestionRun) -> RunOut:
    return RunOut(
        id=run.id, status=run.status.value, started_at=run.started_at,
        finished_at=run.finished_at, error_message=run.error_message, stats=run.stats,
    )  # fmt: skip


def recent_runs(session: Session, source: DataSource, limit: int) -> list[IngestionRun]:
    query = (
        select(IngestionRun)
        .where(IngestionRun.data_source_id == source.id)
        .order_by(IngestionRun.started_at.desc())
        .limit(limit)
    )
    return list(session.scalars(query))


def source_out(session: Session, source: DataSource) -> SourceOut:
    settings = scan_settings(source)
    severities = session.scalars(
        select(Finding.severity).where(
            Finding.data_source_id == source.id, Finding.status.in_(ACTIVE_FINDING_STATUSES)
        )
    )
    latest = _source_snapshots(session, source, 1)
    runs = recent_runs(session, source, 1)
    return SourceOut(
        name=source.name,
        kind=source.kind.value,
        connection_ref=source.connection_ref,
        run_at=settings.run_at,
        timezone=settings.timezone,
        created_at=source.created_at,
        disabled=source.disabled_at is not None,
        baseline_done=source.baseline_completed_at is not None,
        health_score=latest[0].score if latest else None,
        open_findings=dict(sorted(Counter(s.value for s in severities).items())),
        last_run=run_out(runs[0]) if runs else None,
    )


# --- health --------------------------------------------------------------------------------


def _source_snapshots(session: Session, source: DataSource, limit: int) -> list[HealthSnapshot]:
    query = (
        select(HealthSnapshot)
        .where(
            HealthSnapshot.data_source_id == source.id,
            HealthSnapshot.scope == HealthScope.SOURCE,
        )
        .order_by(HealthSnapshot.computed_at.desc())
        .limit(limit)
    )
    return list(session.scalars(query))


def health_out(session: Session, source: DataSource) -> HealthOut:
    """The latest stored health score (from the last run); the API never recomputes it."""
    snapshots = _source_snapshots(session, source, 2)
    if not snapshots:
        raise NotFoundError(f"no health score for {source.name!r} yet; it comes with the first run")
    latest, earlier = snapshots[0], snapshots[1:]
    rows = session.scalars(
        select(HealthSnapshot).where(
            HealthSnapshot.data_source_id == source.id,
            HealthSnapshot.computed_at == latest.computed_at,
            HealthSnapshot.scope != HealthScope.SOURCE,
        )
    ).all()
    schemas = {r.schema_name or "": _score(r) for r in rows if r.scope is HealthScope.SCHEMA}
    tables = [
        TableScoreOut(table=table_ref(session, r.asset_key) or "", **_score(r).model_dump())
        for r in rows
        if r.scope is HealthScope.ASSET
    ]
    return HealthOut(
        computed_at=latest.computed_at,
        source=_score(latest),
        previous_score=earlier[0].score if earlier else None,
        schemas=dict(sorted(schemas.items())),
        tables=sorted(tables, key=lambda t: (t.score, t.table)),
    )


def _score(s: HealthSnapshot) -> ScoreOut:
    return ScoreOut(score=s.score, worst_table=s.min_child_score, open_findings=s.open_findings)


# --- findings ------------------------------------------------------------------------------


def finding_out(session: Session, f: Finding, source_name: str) -> FindingOut:
    return FindingOut(
        id=f.id, source=source_name, category=f.category.value, check_name=f.check_name,
        severity=f.severity.value, status=f.status.value, origin=f.origin.value, title=f.title,
        table=table_ref(session, f.asset_key), column=column_name(session, f.column_key),
        confidence=f.confidence, first_detected_at=f.first_detected_at,
        last_detected_at=f.last_detected_at,
    )  # fmt: skip


def finding_detail(session: Session, f: Finding) -> FindingDetail:
    source = session.get_one(DataSource, f.data_source_id)
    events = session.scalars(
        select(FindingEvent).where(FindingEvent.finding_id == f.id).order_by(FindingEvent.at)
    )
    return FindingDetail(
        **finding_out(session, f, source.name).model_dump(),
        description=f.description,
        evidence={k: v for k, v in f.evidence.items() if k != "explanation"},
        explanation=f.evidence.get("explanation"),
        resolved_at=f.resolved_at,
        reviewed_by=f.reviewed_by,
        reviewed_at=f.reviewed_at,
        review_note=f.review_note,
        history=[_event(e) for e in events],
    )


def _event(e: FindingEvent) -> EventOut:
    before = e.from_status.value if e.from_status else None
    return EventOut(at=e.at, by=e.actor, from_status=before, to_status=e.to_status.value,
                    note=e.note)  # fmt: skip


# --- rules and relationships ---------------------------------------------------------------


def rule_out(session: Session, r: Rule) -> RuleOut:
    return RuleOut(
        id=r.id, source=session.get_one(DataSource, r.data_source_id).name, name=r.name,
        kind=r.rule_type, status=r.status.value, origin=r.origin.value, confidence=r.confidence,
        spec=r.definition["spec"], rationale=r.evidence.get("rationale"), created_at=r.created_at,
        created_by=r.created_by, reviewed_by=r.reviewed_by, reviewed_at=r.reviewed_at,
        history=r.evidence.get("review_history", []),
    )  # fmt: skip


def relationship_out(session: Session, rel: Relationship) -> RelationshipOut:
    column = session.get_one
    pairs = [
        (column(AssetColumn, p.from_column_key), column(AssetColumn, p.to_column_key))
        for p in rel.columns
    ]
    return RelationshipOut(
        id=rel.id,
        from_table=table_ref(session, rel.from_asset_key) or "",
        from_columns=[child.name for child, _ in pairs],
        to_table=table_ref(session, rel.to_asset_key) or "",
        to_columns=[parent.name for _, parent in pairs],
        kind=rel.kind.value,
        status=rel.status.value,
        confidence=rel.confidence,
        signals=list(rel.signals),
        ai_review=rel.evidence.get("ai_review"),
        reviewed_by=rel.reviewed_by,
    )
