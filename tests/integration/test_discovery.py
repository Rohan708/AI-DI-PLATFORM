"""Relationship discovery, relationship checks and generated docs, against the real lab."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ai_data_engineer.db import create_db_engine
from ai_data_engineer.discovery.discover import discover
from ai_data_engineer.discovery.docs import generate_docs
from ai_data_engineer.discovery.store import review
from ai_data_engineer.graph.models import (
    DataSource,
    Finding,
    FindingStatus,
    Relationship,
    RelationshipKind,
    RelationshipStatus,
)
from ai_data_engineer.ingestion.base import KeyRef
from ai_data_engineer.ingestion.postgres import PostgresAdapter
from ai_data_engineer.ingestion.scan import scan_source
from ai_data_engineer.ingestion.settings import ScanSettings
from ai_data_engineer.lab.answer_key import AnswerKey
from ai_data_engineer.lab.runner import build_lab, inject, lab_scan_time, run_plan, tick
from ai_data_engineer.lab.scenarios import ORPHAN_ID_OFFSET
from ai_data_engineer.lab.scorer import load_findings, load_relationships, score

SETTINGS = ScanSettings(exclude_schemas=("aide_lab",))
T1 = datetime(2026, 4, 1, 3, tzinfo=UTC)
# Stage 1.4 "done when": at least 13 of the 15 true relationships, at most 2 wrong.
MIN_RELATIONSHIPS_FOUND = 13
MAX_WRONG_RELATIONSHIPS = 2


def _factory(url: str) -> Callable[[DataSource], Any]:
    return lambda _source: PostgresAdapter(url, SETTINGS)


def _build(make_database: Callable[[], str], seed: int, days: int = 2) -> str:
    url = make_database()
    engine = create_db_engine(url)
    build_lab(engine, seed=seed, size="tiny")
    tick(engine, days)
    engine.dispose()
    return url


def _scan_and_discover(session: Session, source: DataSource, url: str, at: datetime) -> Any:
    scan_source(session, source, observed_at=at, adapter_factory=_factory(url))
    return discover(session, source, adapter_factory=_factory(url))


def _empty_key() -> AnswerKey:
    return AnswerKey(seed=0, size="tiny", current_day=date(2026, 1, 1), anomalies=())


@pytest.fixture(scope="module")
def clean_lab_url(make_database: Callable[[], str]) -> str:
    return _build(make_database, seed=21)


# --- adapter building blocks ------------------------------------------------------------------


def test_query_log_shows_the_application_joins(clean_lab_url: str) -> None:
    with PostgresAdapter(clean_lab_url, SETTINGS) as adapter:
        stats = adapter.read_query_log(1000)
    assert any("cust_no" in s.query and s.calls > 0 for s in stats)


def test_value_inclusion_and_orphan_count(clean_lab_url: str) -> None:
    orders = KeyRef("shop", "orders", ("cust_no",))
    customers = KeyRef("shop", "customers", ("id",))
    with PostgresAdapter(clean_lab_url, SETTINGS) as adapter:
        overlap = adapter.value_inclusion(orders, customers, max_rows=10_000)
        orphans = adapter.count_orphans(orders, customers, ("order_id",), sample_size=5)
    assert overlap.checked_rows > 0
    assert overlap.inclusion == 1.0
    assert (orphans.orphan_rows, orphans.sample) == (0, [])


# --- discovery on the clean lab ----------------------------------------------------------------


def test_discovers_declared_and_hidden_relationships(
    session: Session, source: DataSource, clean_lab_url: str
) -> None:
    result = _scan_and_discover(session, source, clean_lab_url, T1)

    report = score(_empty_key(), [], load_relationships(session, source.id))
    assert result.declared == 4
    assert len(report.relationships_found) >= MIN_RELATIONSHIPS_FOUND, report.to_markdown()
    assert len(report.relationships_false) <= MAX_WRONG_RELATIONSHIPS, report.to_markdown()
    assert result.checks.opened == 0  # clean data: no orphans


def test_rediscovery_is_stable_and_keeps_human_reviews(
    session: Session, source: DataSource, clean_lab_url: str
) -> None:
    _scan_and_discover(session, source, clean_lab_url, T1)
    inferred = session.scalars(
        select(Relationship).where(Relationship.kind == RelationshipKind.INFERRED)
    ).all()
    confirmed, rejected = inferred[0], inferred[1]
    review(session, confirmed, RelationshipStatus.CONFIRMED, "tester", T1)
    review(session, rejected, RelationshipStatus.REJECTED, "tester", T1)
    before = session.scalar(select(func.count()).select_from(Relationship))

    again = _scan_and_discover(session, source, clean_lab_url, T1 + timedelta(days=1))

    assert again.sync.created == []
    assert again.sync.retired == []
    assert session.scalar(select(func.count()).select_from(Relationship)) == before
    assert session.get_one(Relationship, confirmed.id).status is RelationshipStatus.CONFIRMED
    assert session.get_one(Relationship, rejected.id).status is RelationshipStatus.REJECTED


def test_generated_docs(
    session: Session, source: DataSource, clean_lab_url: str, tmp_path: Path
) -> None:
    _scan_and_discover(session, source, clean_lab_url, T1)

    paths = {
        p.name: p.read_text(encoding="utf-8") for p in generate_docs(session, source, tmp_path)
    }

    assert set(paths) == {"README.md", "data_dictionary.md", "relationships.md"}
    assert "erDiagram" in paths["README.md"]
    assert "shop_customers ||..o{ shop_orders" in paths["README.md"]  # inferred: dotted
    assert "shop_customers ||--o{ shop_addresses" in paths["README.md"]  # declared: solid
    assert "## `shop.orders`" in paths["data_dictionary.md"]
    assert "inferred" in paths["relationships.md"]


# --- orphans: found, kept up to date, resolved ------------------------------------------------


def test_orphan_finding_lifecycle(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url = _build(make_database, seed=23)
    lab = create_db_engine(url)
    _scan_and_discover(session, source, url, T1)
    inject(lab, ["orphan_orders"])

    first = _scan_and_discover(session, source, url, T1 + timedelta(days=1))
    second = _scan_and_discover(session, source, url, T1 + timedelta(days=2))

    findings = session.scalars(select(Finding).where(Finding.check_name == "orphan_rows")).all()
    assert (first.checks.opened, second.checks.refreshed) == (1, 1)
    assert len(findings) == 1
    orphan = findings[0]
    assert orphan.evidence["orphan_rows"] > 0
    assert orphan.evidence["sample_rows"]  # offending order ids are attached
    assert orphan.last_detected_at == T1 + timedelta(days=2)

    with lab.begin() as conn:  # someone fixes the data
        conn.execute(
            text("UPDATE shop.orders SET cust_no = cust_no - :o WHERE cust_no >= :o"),
            {"o": ORPHAN_ID_OFFSET},
        )
    fixed = _scan_and_discover(session, source, url, T1 + timedelta(days=3))
    lab.dispose()

    assert fixed.checks.resolved == 1
    assert session.get_one(Finding, orphan.id).status is FindingStatus.RESOLVED


# --- the benchmark: planted lab, nightly scans, then discovery --------------------------------


def test_benchmark_catches_orphans_and_finds_relationships(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url = make_database()
    lab = create_db_engine(url)

    def nightly(day: date) -> None:
        scan_source(session, source, observed_at=lab_scan_time(day), adapter_factory=_factory(url))

    key = run_plan(lab, "quick", seed=22, size="tiny", on_day_end=nightly)
    discover(session, source, adapter_factory=_factory(url))
    lab.dispose()

    report = score(key, load_findings(session, source.id), load_relationships(session, source.id))
    caught = {anomaly.scenario for anomaly, _ in report.caught}
    assert "orphan_orders" in caught, report.to_markdown()
    assert len(report.relationships_found) >= MIN_RELATIONSHIPS_FOUND, report.to_markdown()
    assert len(report.relationships_false) <= MAX_WRONG_RELATIONSHIPS, report.to_markdown()
