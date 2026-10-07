"""Detection against the real lab: the Stage 1.5 benchmark, cold start, resolution,
idempotency, and the query-log memory / not-re-checked fixes from 1.4."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from ai_data_engineer.db import create_db_engine
from ai_data_engineer.detection.runner import detect
from ai_data_engineer.discovery.discover import discover
from ai_data_engineer.discovery.settings import DiscoverySettings
from ai_data_engineer.graph.models import (
    AssetColumn,
    DataSource,
    Finding,
    FindingStatus,
    QueryJoin,
    Relationship,
    RelationshipColumn,
)
from ai_data_engineer.ingestion.postgres import PostgresAdapter
from ai_data_engineer.ingestion.scan import scan_source
from ai_data_engineer.ingestion.settings import ScanSettings
from ai_data_engineer.lab.runner import build_lab, inject, lab_scan_time, run_plan, tick
from ai_data_engineer.lab.scorer import load_findings, load_relationships, score

SETTINGS = ScanSettings(exclude_schemas=("aide_lab",))
T1 = datetime(2026, 5, 1, 3, tzinfo=UTC)
STAGE_ONE = ("1.4", "1.5")
MAX_FALSE_ALARMS = 2  # Stage 1.5 "done when": all caught, false alarms few and explainable


def _factory(url: str) -> Callable[[DataSource], Any]:
    return lambda _source: PostgresAdapter(url, SETTINGS)


def _scan(session: Session, source: DataSource, url: str, at: datetime) -> None:
    scan_source(session, source, observed_at=at, adapter_factory=_factory(url))


def _lab(make_database: Callable[[], str], seed: int) -> tuple[str, Engine]:
    url = make_database()
    engine = create_db_engine(url)
    build_lab(engine, seed=seed, size="tiny")
    return url, engine


# --- the benchmark --------------------------------------------------------------------------


def test_benchmark_catches_every_stage_one_anomaly(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url = make_database()
    lab = create_db_engine(url)

    def nightly(day: date) -> None:
        _scan(session, source, url, lab_scan_time(day))

    key = run_plan(lab, "standard", seed=31, size="tiny", on_day_end=nightly)
    lab.dispose()
    discover(session, source, adapter_factory=_factory(url))
    detect(session, source)

    report = score(key, load_findings(session, source.id), load_relationships(session, source.id))
    missed = [a.scenario for a in report.missed if a.detect_stage in STAGE_ONE]
    assert missed == [], report.to_markdown()
    assert len(report.false_alarms) <= MAX_FALSE_ALARMS, report.to_markdown()
    assert len(report.known_missed) <= 1, report.to_markdown()


# --- behaviour ---------------------------------------------------------------------------------


def test_cold_start_learns_instead_of_guessing(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url, lab = _lab(make_database, seed=32)
    tick(lab, 2, on_day_end=lambda day: _scan(session, source, url, lab_scan_time(day)))
    lab.dispose()

    result = detect(session, source)

    assert result.learning > 0
    statistical = {
        "null_rate_spike",
        "out_of_range",
        "inconsistent_categories",
        "duplicate_values",
        "volume_drop",
        "stale_table",
    }
    assert all(result.by_check[c].opened == 0 for c in statistical)


def test_detection_is_idempotent_and_conditions_resolve(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url, lab = _lab(make_database, seed=33)
    _scan(session, source, url, T1)
    first = detect(session, source)
    second = detect(session, source)
    total = session.scalar(select(func.count()).select_from(Finding))

    assert first.by_check["missing_primary_key"].opened == 1  # legacy.INV_LINE_TAX
    assert second.opened == 0
    assert second.refreshed == first.opened
    assert total == first.opened

    with lab.begin() as conn:  # someone adds the missing key
        conn.execute(
            text('ALTER TABLE legacy."INV_LINE_TAX" ADD PRIMARY KEY ("INV_NO", "LINE_NO")')
        )
    lab.dispose()
    _scan(session, source, url, T1 + timedelta(days=1))
    third = detect(session, source)

    assert third.by_check["missing_primary_key"].resolved == 1


# --- fixes carried over from 1.4 ---------------------------------------------------------------


def test_query_log_evidence_survives_a_statistics_reset(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url, lab = _lab(make_database, seed=34)
    _scan(session, source, url, T1)
    discover(session, source, adapter_factory=_factory(url))
    before = _confidence(session, "cust_no")

    with lab.begin() as conn:  # what a Postgres restart does to the statistics
        conn.execute(text("SELECT pg_stat_statements_reset()"))
    lab.dispose()
    _scan(session, source, url, T1 + timedelta(days=1))
    discover(session, source, adapter_factory=_factory(url))

    assert (session.scalar(select(func.count()).select_from(QueryJoin)) or 0) > 0
    assert _confidence(session, "cust_no") == before


def test_orphan_finding_says_when_it_could_not_be_rechecked(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url, lab = _lab(make_database, seed=35)
    inject(lab, ["orphan_orders"])
    tick(lab, 1)
    lab.dispose()
    _scan(session, source, url, T1)
    discover(session, source, adapter_factory=_factory(url))

    strict = DiscoverySettings(orphan_check_min_confidence=0.995)
    result = discover(session, source, adapter_factory=_factory(url), settings=strict)

    orphan = session.scalars(select(Finding).where(Finding.check_name == "orphan_rows")).one()
    assert result.checks.not_rechecked == 1
    assert orphan.status is FindingStatus.OPEN
    assert orphan.evidence["rechecked"] is False
    assert "below" in orphan.evidence["not_rechecked_reason"]


def _confidence(session: Session, child_column: str) -> float:
    rel = session.scalars(
        select(Relationship)
        .join(RelationshipColumn, RelationshipColumn.relationship_id == Relationship.id)
        .join(AssetColumn, AssetColumn.column_key == RelationshipColumn.from_column_key)
        .where(AssetColumn.name == child_column, Relationship.valid_to.is_(None))
    ).one()
    assert rel.confidence is not None
    return rel.confidence
