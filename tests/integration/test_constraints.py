"""The database itself enforces the core invariants, not just application code."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_data_engineer.graph.models import (
    AssetKind,
    AssetVersion,
    ColumnProfile,
    DataSource,
    Finding,
    FindingCategory,
    FindingStatus,
    IngestionRun,
    Origin,
    Relationship,
    RelationshipKind,
    RelationshipStatus,
    Rule,
    RuleStatus,
    Severity,
    Tenant,
    TypeFamily,
    finding_fingerprint,
)
from ai_data_engineer.graph.queries import get_current_structure
from ai_data_engineer.graph.versioning import AssetObservation, ColumnObservation, record_asset

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _fails(session: Session, obj: object) -> None:
    savepoint = session.begin_nested()
    session.add(obj)
    with pytest.raises(IntegrityError):
        session.flush()
    savepoint.rollback()


def _asset(session: Session, run: IngestionRun) -> tuple[uuid.UUID, uuid.UUID]:
    """Record a one-column table; return (asset_key, column_key)."""
    result = record_asset(
        session,
        run,
        AssetObservation(
            namespace="shop",
            schema_name="public",
            name="orders",
            kind=AssetKind.TABLE,
            columns=(ColumnObservation("id", "integer", TypeFamily.INTEGER, False, 1),),
        ),
        observed_at=T0,
    )
    [current] = get_current_structure(session, run.data_source_id)
    return result.asset_key, current.columns[0].column_key


def _finding(source: DataSource, fingerprint: str, **overrides: object) -> Finding:
    fields: dict[str, object] = {
        "tenant_id": source.tenant_id,
        "data_source_id": source.id,
        "category": FindingCategory.COLUMN_VALUE,
        "check_name": "null_rate_spike",
        "fingerprint": fingerprint,
        "severity": Severity.HIGH,
        "title": "Null spike",
        "description": "null rate rose from 0.03% to 2.0%",
    }
    fields.update(overrides)
    return Finding(**fields)


def test_only_one_current_version_per_asset(session: Session, run: IngestionRun) -> None:
    asset_key, _ = _asset(session, run)
    duplicate = AssetVersion(
        tenant_id=run.tenant_id,
        asset_key=asset_key,
        ingestion_run_id=run.id,
        valid_from=T0 + timedelta(days=1),
        kind=AssetKind.TABLE,
    )
    _fails(session, duplicate)


def test_version_cannot_end_before_it_starts(session: Session, run: IngestionRun) -> None:
    asset_key, _ = _asset(session, run)
    backwards = AssetVersion(
        tenant_id=run.tenant_id,
        asset_key=asset_key,
        ingestion_run_id=run.id,
        valid_from=T0,
        valid_to=T0 - timedelta(seconds=1),
        kind=AssetKind.TABLE,
    )
    _fails(session, backwards)


def test_tenant_id_is_required(session: Session) -> None:
    _fails(session, DataSource(name="no-tenant", kind="postgres"))


def test_one_profile_per_column_per_run(session: Session, run: IngestionRun) -> None:
    _, column_key = _asset(session, run)
    session.add(
        ColumnProfile(
            tenant_id=run.tenant_id, column_key=column_key, ingestion_run_id=run.id, measured_at=T0
        )
    )
    session.flush()
    _fails(
        session,
        ColumnProfile(
            tenant_id=run.tenant_id, column_key=column_key, ingestion_run_id=run.id, measured_at=T0
        ),
    )


def test_null_rate_must_be_between_0_and_1(session: Session, run: IngestionRun) -> None:
    _, column_key = _asset(session, run)
    _fails(
        session,
        ColumnProfile(
            tenant_id=run.tenant_id,
            column_key=column_key,
            ingestion_run_id=run.id,
            measured_at=T0,
            null_rate=1.5,
        ),
    )


def test_ai_rule_requires_confidence(session: Session, source: DataSource) -> None:
    def rule(confidence: float | None) -> Rule:
        return Rule(
            tenant_id=source.tenant_id,
            data_source_id=source.id,
            name="ship after order",
            rule_type="sql_assertion",
            definition={"sql": "ship_date >= order_date"},
            origin=Origin.AI,
            status=RuleStatus.PROPOSED,
            confidence=confidence,
        )

    _fails(session, rule(None))
    session.add(rule(0.87))
    session.flush()


def test_ai_finding_requires_confidence(session: Session, source: DataSource) -> None:
    _fails(session, _finding(source, "fp-ai", origin=Origin.AI))
    session.add(_finding(source, "fp-ai", origin=Origin.AI, confidence=0.9))
    session.flush()


def test_inferred_relationship_requires_confidence(session: Session, run: IngestionRun) -> None:
    asset_key, _ = _asset(session, run)
    _fails(
        session,
        Relationship(
            tenant_id=run.tenant_id,
            from_asset_key=asset_key,
            to_asset_key=asset_key,
            kind=RelationshipKind.INFERRED,
            status=RelationshipStatus.PROPOSED,
            signature="x" * 64,
            valid_from=T0,
        ),
    )


def test_one_active_finding_per_fingerprint(session: Session, source: DataSource) -> None:
    fingerprint = finding_fingerprint("null_rate_spike", uuid.uuid4())
    first = _finding(source, fingerprint)
    session.add(first)
    session.flush()

    _fails(session, _finding(source, fingerprint))

    # Once resolved, the same problem may be reported again as a new finding.
    first.status = FindingStatus.RESOLVED
    session.flush()
    session.add(_finding(source, fingerprint))
    session.flush()


def test_same_fingerprint_allowed_in_another_tenant(
    session: Session,
    source: DataSource,
    make_tenant: Callable[[], Tenant],
    make_source: Callable[[Tenant], DataSource],
) -> None:
    other_source = make_source(make_tenant())
    session.add_all([_finding(source, "shared"), _finding(other_source, "shared")])
    session.flush()
