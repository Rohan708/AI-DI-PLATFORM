"""Stage 1.6 against the real lab: review decisions stick, health scores, quiet baseline,
digests without repeats, the full nightly pipeline, and removing a source."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_data_engineer.alerting.notifier import AlertMessage
from ai_data_engineer.alerting.router import send_alerts
from ai_data_engineer.db import create_db_engine
from ai_data_engineer.detection.recording import change_status, record_finding
from ai_data_engineer.detection.runner import detect
from ai_data_engineer.graph.models import (
    DataSource,
    Finding,
    FindingCategory,
    FindingEvent,
    FindingStatus,
    HealthScope,
    HealthSnapshot,
    IngestionRun,
    Severity,
    SourceKind,
    Tenant,
)
from ai_data_engineer.health_score.engine import compute_health
from ai_data_engineer.ingestion.postgres import PostgresAdapter
from ai_data_engineer.ingestion.scan import scan_source
from ai_data_engineer.ingestion.settings import ScanSettings
from ai_data_engineer.ingestion.sources import remove_source
from ai_data_engineer.lab.runner import build_lab, lab_scan_time, run_plan
from ai_data_engineer.pipeline import run_pipeline

SETTINGS = ScanSettings(exclude_schemas=("aide_lab",))
T1 = datetime(2026, 6, 1, 3, tzinfo=UTC)


class FakeNotifier:
    """Collects messages instead of calling Slack (no external calls in tests)."""

    def __init__(self, fail: bool = False) -> None:
        self.messages: list[AlertMessage] = []
        self.fail = fail

    def send(self, message: AlertMessage) -> None:
        if self.fail:
            raise OSError("slack unreachable")
        self.messages.append(message)


def _factory(url: str) -> Callable[[DataSource], Any]:
    return lambda _source: PostgresAdapter(url, SETTINGS)


def _scanned_lab(
    session: Session, source: DataSource, make_database: Callable[[], str], seed: int
) -> str:
    url = make_database()
    engine = create_db_engine(url)
    build_lab(engine, seed=seed, size="tiny")
    engine.dispose()
    scan_source(session, source, observed_at=T1, adapter_factory=_factory(url))
    return url


def _missing_pk_finding(session: Session) -> Finding:
    return session.scalars(select(Finding).where(Finding.check_name == "missing_primary_key")).one()


# --- lifecycle -------------------------------------------------------------------------------


def test_a_rejected_finding_is_not_raised_again(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    _scanned_lab(session, source, make_database, seed=41)
    detect(session, source)
    finding = _missing_pk_finding(session)

    change_status(
        session, finding, FindingStatus.REJECTED, actor="alice", now=T1, note="legacy, by design"
    )
    again = detect(session, source)

    assert again.by_check["missing_primary_key"].suppressed == 1
    assert again.by_check["missing_primary_key"].opened == 0
    assert _missing_pk_finding(session).status is FindingStatus.REJECTED
    history = session.scalars(
        select(FindingEvent).where(FindingEvent.finding_id == finding.id).order_by(FindingEvent.at)
    ).all()
    assert [(e.from_status, e.to_status, e.actor) for e in history] == [
        (None, FindingStatus.OPEN, "aide"),
        (FindingStatus.OPEN, FindingStatus.REJECTED, "alice"),
    ]
    assert finding.review_note == "legacy, by design"


# --- health ----------------------------------------------------------------------------------


def test_health_scores_follow_open_findings(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    _scanned_lab(session, source, make_database, seed=42)
    detect(session, source)

    report = compute_health(session, source, T1)

    scores = {t.ref: t.score.score for t in report.tables}
    assert scores["legacy.INV_LINE_TAX"] == 90  # one medium finding: no primary key
    assert scores["shop.orders"] == 100
    assert report.source.min_child == 90
    assert 90 < report.source.score < 100
    assert report.schemas["legacy"].min_child == 90
    stored = session.scalar(select(func.count()).select_from(HealthSnapshot))
    assert stored == len(report.tables) + len(report.schemas) + 1

    later = compute_health(session, source, T1 + timedelta(hours=1))
    assert later.previous_source_score == report.source.score
    assert later.change == 0


# --- alerts ----------------------------------------------------------------------------------


def _new_finding(session: Session, source: DataSource, severity: Severity, title: str) -> None:
    record_finding(
        session,
        source=source,
        fingerprint=title,
        category=FindingCategory.COLUMN_VALUE,
        check_name="test_check",
        severity=severity,
        title=title,
        description="d",
        evidence={},
        now=T1,
    )


def test_quiet_baseline_then_one_digest_without_repeats(
    session: Session, source: DataSource
) -> None:
    notifier = FakeNotifier()
    _new_finding(session, source, Severity.HIGH, "existing mess")

    first = send_alerts(session, source, notifier, T1)
    assert first.quiet_baseline
    assert first.baselined == 1
    assert notifier.messages == []

    _new_finding(session, source, Severity.MEDIUM, "nulls jumped")
    _new_finding(session, source, Severity.LOW, "unindexed column")  # below the threshold
    second = send_alerts(session, source, notifier, T1 + timedelta(days=1))
    assert second.sent == 1
    assert len(notifier.messages) == 1
    assert "nulls jumped" in notifier.messages[0].as_text()
    assert "unindexed column" not in notifier.messages[0].as_text()

    third = send_alerts(session, source, notifier, T1 + timedelta(days=2))
    assert third.sent == 0
    assert len(notifier.messages) == 1  # nothing alerted twice


def test_failed_send_is_retried_next_run(session: Session, source: DataSource) -> None:
    send_alerts(session, source, FakeNotifier(), T1)  # quiet baseline
    _new_finding(session, source, Severity.HIGH, "stale table")

    failed = send_alerts(session, source, FakeNotifier(fail=True), T1 + timedelta(days=1))
    assert failed.error is not None
    assert "unreachable" in failed.error

    working = FakeNotifier()
    retried = send_alerts(session, source, working, T1 + timedelta(days=2))
    assert retried.sent == 1
    assert len(working.messages) == 1


# --- the nightly pipeline ----------------------------------------------------------------------


def test_nightly_pipeline_tells_the_right_story(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url = make_database()
    lab = create_db_engine(url)
    notifier = FakeNotifier()
    per_night: dict[date, int] = {}
    health: dict[date, float] = {}

    sent_on: dict[date, list[str]] = {}

    def nightly(day: date) -> None:
        before = len(notifier.messages)
        result = run_pipeline(
            session, source, observed_at=lab_scan_time(day), notifier=notifier,
            adapter_factory=_factory(url),
        )  # fmt: skip
        assert result.ok, result.summary()
        per_night[day] = len(notifier.messages) - before
        sent_on[day] = [m.as_text() for m in notifier.messages[before:]]
        latest = session.scalars(
            select(HealthSnapshot)
            .where(HealthSnapshot.scope == HealthScope.SOURCE)
            .order_by(HealthSnapshot.computed_at.desc())
        ).first()
        assert latest is not None
        health[day] = latest.score

    key = run_plan(lab, "standard", seed=43, size="tiny", on_day_end=nightly)
    lab.dispose()

    nights = sorted(per_night)
    first, injection = nights[0], key.current_day
    assert per_night[first] == 0  # quiet baseline: a messy start alerts nothing
    early = {n: sent_on[n] for n in nights if first < n < injection and sent_on[n]}
    assert sum(len(m) for m in early.values()) <= 1, early  # quiet nights
    assert per_night[injection] == 1  # one digest with the planted problems
    digest = notifier.messages[-1].as_text()
    assert "postal_code" in digest
    assert "weight_grams" in digest
    assert health[injection] < health[nights[-2]]  # the score dropped

    again = run_pipeline(
        session, source, observed_at=lab_scan_time(injection) + timedelta(minutes=5),
        notifier=notifier, adapter_factory=_factory(url),
    )  # fmt: skip
    assert again.ok
    assert notifier.messages[-1].as_text() == digest  # no new digest


def test_pipeline_stops_after_a_failed_scan(session: Session, source: DataSource) -> None:
    dead = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none"
    result = run_pipeline(session, source, observed_at=T1, adapter_factory=_factory(dead))

    assert not result.ok
    assert [s.step for s in result.steps] == ["scan"]
    skipped = "skipped: discover, rules, rows, dbhealth, detect, reconcile, health, alert"
    assert skipped in result.summary()
    run = session.scalars(
        select(IngestionRun).where(IngestionRun.data_source_id == source.id)
    ).one()
    assert run.error_message  # the failed run is kept on record


# --- housekeeping -----------------------------------------------------------------------------


def test_remove_source_deletes_only_that_source(
    session: Session, source: DataSource, make_database: Callable[[], str], tenant: Tenant
) -> None:
    url = _scanned_lab(session, source, make_database, seed=44)
    detect(session, source)
    other = DataSource(tenant_id=tenant.id, name="other", kind=SourceKind.POSTGRES)
    session.add(other)
    session.flush()
    scan_source(session, other, observed_at=T1, adapter_factory=_factory(url))

    counts = remove_source(session, source)

    assert counts["asset"] > 0
    assert counts["finding"] > 0
    assert session.get(DataSource, source.id) is None
    remaining = session.scalars(select(IngestionRun.data_source_id)).all()
    assert set(remaining) == {other.id}
