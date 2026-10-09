"""Stage 1.6 logic without a database: score maths, digests, Slack text, status rules."""

from typing import cast

import pytest
from sqlalchemy.orm import Session

from ai_data_engineer.alerting.notifier import AlertMessage, SlackNotifier, slack_text
from ai_data_engineer.alerting.router import AlertSettings, build_digest
from ai_data_engineer.detection.recording import InvalidTransitionError, change_status
from ai_data_engineer.graph.models import (
    DataSource,
    Finding,
    FindingStatus,
    Severity,
    SourceKind,
    utcnow,
)
from ai_data_engineer.health_score.score import group_score, table_score
from ai_data_engineer.pipeline import STEPS, PipelineResult, StepOutcome


def test_table_score_subtracts_penalties_with_a_floor() -> None:
    assert table_score([]).score == 100
    healthy_but_two_issues = table_score([Severity.HIGH, Severity.MEDIUM])
    assert healthy_but_two_issues.score == 70
    assert healthy_but_two_issues.open_findings == {"high": 1, "medium": 1}
    assert table_score([Severity.CRITICAL] * 3).score == 0  # never negative
    assert table_score([Severity.INFO]).score == 100  # diagnostics are free


def test_group_score_is_the_average_and_keeps_the_worst() -> None:
    group = group_score([table_score([]), table_score([]), table_score([Severity.CRITICAL])])
    assert group.score == pytest.approx(86.7, abs=0.1)
    assert group.min_child == 60
    assert group_score([]).score == 100


def _finding(severity: Severity, title: str, check: str = "c") -> Finding:
    return Finding(severity=severity, title=title, check_name=check)


def test_digest_counts_by_severity_and_lists_the_worst_first() -> None:
    source = DataSource(name="shopco", kind=SourceKind.POSTGRES)
    findings = [_finding(Severity.HIGH, "orders stale"), _finding(Severity.MEDIUM, "nulls")]
    digest = build_digest(source, findings)
    assert digest.title == "AI Data Engineer: 2 new problems in shopco (1 high, 1 medium)"
    assert digest.lines == ["[high] orders stale", "[medium] nulls"]


def test_digest_truncates_long_lists() -> None:
    source = DataSource(name="shopco", kind=SourceKind.POSTGRES)
    findings = [_finding(Severity.MEDIUM, f"problem {i}", check=f"check_{i}") for i in range(5)]
    digest = build_digest(source, findings, AlertSettings(max_items=2))
    assert len(digest.lines) == 3
    assert digest.lines[-1].startswith("... and 3 more")


def test_digest_groups_one_incident_spilling_into_many_tables() -> None:
    source = DataSource(name="shopco", kind=SourceKind.POSTGRES)
    findings = [
        _finding(Severity.HIGH, f"t{i} gained few rows", check="volume_drop") for i in range(8)
    ]
    findings.append(_finding(Severity.MEDIUM, "nulls jumped", check="null_rate_spike"))
    digest = build_digest(source, findings)
    assert digest.title.endswith("(8 high, 1 medium)")
    assert digest.lines == [
        "[high] 8 x volume_drop: t0 gained few rows (and 7 more like it)",
        "[medium] nulls jumped",
    ]


def test_slack_text_and_https_only() -> None:
    text = slack_text(AlertMessage("Title", ["a", "b"]))
    assert text == "*Title*\n• a\n• b"
    with pytest.raises(ValueError, match="https"):
        SlackNotifier("http://hooks.example/insecure")


def test_status_rules_are_enforced() -> None:
    rejected = Finding(status=FindingStatus.REJECTED, title="t")
    with pytest.raises(InvalidTransitionError, match="rejected can't become resolved"):
        change_status(
            cast(Session, None), rejected, FindingStatus.RESOLVED, actor="me", now=utcnow()
        )


def test_history_keeps_one_scan_per_day() -> None:
    from datetime import UTC, datetime, timedelta

    from ai_data_engineer.detection.context import daily
    from ai_data_engineer.graph.models import AssetProfile

    night = datetime(2026, 3, 1, 3, tzinfo=UTC)
    scans = [
        AssetProfile(measured_at=night),
        AssetProfile(measured_at=night + timedelta(days=1)),
        AssetProfile(measured_at=night + timedelta(days=1, minutes=5)),  # a same-day re-run
    ]
    kept = daily(scans, min_gap_hours=20)
    assert [p.measured_at for p in kept] == [night, night + timedelta(days=1, minutes=5)]


def test_pipeline_result_reports_skipped_steps() -> None:
    result = PipelineResult("shopco", [StepOutcome("scan", False, "boom")])
    assert not result.ok
    skipped = "skipped: discover, rules, rows, dbhealth, detect, reconcile, health, alert"
    assert skipped in result.summary()
    full = PipelineResult(
        "shopco",
        [StepOutcome(s, True, "") for s in STEPS],
    )
    assert full.ok
