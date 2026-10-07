"""Detection logic that needs no database: statistics, variant matching, index coverage,
and the benchmark's handling of knock-on findings and known baseline issues."""

from datetime import date

import pytest

from ai_data_engineer.detection.checks.structural import _indexed
from ai_data_engineer.detection.checks.values import looks_like_variant
from ai_data_engineer.detection.stats import robust_z
from ai_data_engineer.graph.models import FindingCategory
from ai_data_engineer.lab.answer_key import AnswerKey, ExpectedAnomaly
from ai_data_engineer.lab.schema import BASELINE_ISSUES
from ai_data_engineer.lab.scorer import FoundFinding, is_knock_on, score


def test_robust_z_uses_a_spread_floor_for_flat_history() -> None:
    flat = [0.0] * 7
    assert robust_z(0.08, flat, min_spread=0.005) == pytest.approx(16.0)
    assert robust_z(0.0, flat, min_spread=0.005) == 0.0


def test_robust_z_ignores_one_odd_day() -> None:
    history = [0.10, 0.11, 0.09, 0.10, 0.50, 0.10, 0.11]  # one outlier in the history
    assert robust_z(0.105, history, min_spread=0.005) < 1


@pytest.mark.parametrize(
    ("value", "known", "expected"),
    [
        ("USA", "US", True),  # prefix
        ("United States", "US", True),  # initials
        ("us ", "US", True),  # same after normalising
        ("Canada", "CA", True),
        ("paid", "placed", False),
        ("GB", "DE", False),
    ],
)
def test_looks_like_variant(value: str, known: str, expected: bool) -> None:
    assert looks_like_variant(value, known) is expected


def test_index_coverage_uses_leading_columns() -> None:
    pk = [{"name": "pk", "columns": ["order_id", "line_no"], "unique": True, "primary": True}]
    assert _indexed(["order_id"], tuple(pk))
    assert not _indexed(["line_no"], tuple(pk))  # not the leading column
    assert _indexed(["line_no", "order_id"], tuple(pk))  # composite, any order
    assert not _indexed(["cust_no"], ())


# --- benchmark scoring ------------------------------------------------------------------------

DAY = date(2026, 2, 1)
DUPLICATES = ExpectedAnomaly(
    "duplicate_customers", FindingCategory.RELATIONAL, "shop.customers", "email", "x", "1.5",
    "d", DAY,
)  # fmt: skip
ORPHANS = ExpectedAnomaly(
    "orphan_orders", FindingCategory.RELATIONAL, "shop.orders", "cust_no", "x", "1.4", "d",
    DAY, related_tables=("reporting.customer_summary",),
)  # fmt: skip
DROP_PK = ExpectedAnomaly(
    "drop_primary_key", FindingCategory.STRUCTURAL, "legacy.INV_LINE", None, "x", "1.5", "d", DAY
)


def _f(check: str, category: FindingCategory, table: str, column: str | None) -> FoundFinding:
    return FoundFinding(category, table, column, check, "t")


def test_knock_on_findings_are_related_not_false_alarms() -> None:
    anomalies = (DUPLICATES, ORPHANS)
    # duplicated customers also duplicate their phone numbers (same table, same category)
    assert is_knock_on(
        anomalies, _f("duplicate_values", FindingCategory.RELATIONAL, "shop.customers", "phone")
    )
    # orphan ids make the key's maximum jump (same column, other category)
    assert is_knock_on(
        anomalies, _f("out_of_range", FindingCategory.COLUMN_VALUE, "shop.orders", "cust_no")
    )
    # and spill into a related table
    assert is_knock_on(
        anomalies,
        _f(
            "out_of_range",
            FindingCategory.COLUMN_VALUE,
            "reporting.customer_summary",
            "customer_id",
        ),
    )
    # an unrelated value finding on the same table is still a false alarm
    assert not is_knock_on(
        anomalies,
        _f("null_rate_spike", FindingCategory.COLUMN_VALUE, "shop.customers", "middle_name"),
    )


def test_baseline_issues_are_matched_first_and_by_check_name() -> None:
    key = AnswerKey(seed=1, size="tiny", current_day=DAY, anomalies=(DROP_PK,), relationships=())
    findings = [
        _f("primary_key_removed", FindingCategory.STRUCTURAL, "legacy.INV_LINE", None),
        _f("unindexed_foreign_key", FindingCategory.STRUCTURAL, "legacy.INV_LINE", "ITEM_CD"),
        _f("missing_primary_key", FindingCategory.STRUCTURAL, "legacy.INV_LINE_TAX", None),
        _f("unindexed_foreign_key", FindingCategory.STRUCTURAL, "legacy.INV_LINE_TAX", None),
    ]
    report = score(key, findings, [])

    found = {(b.table, b.check_hint) for b in report.known_found}
    assert ("legacy.INV_LINE", "unindexed_foreign_key") in found  # not absorbed by DROP_PK
    assert ("legacy.INV_LINE_TAX", "missing_primary_key") in found
    assert ("legacy.INV_LINE_TAX", "unindexed_foreign_key") in found
    assert [a.scenario for a, _ in report.caught] == ["drop_primary_key"]
    assert report.false_alarms == []


def test_baseline_issue_catalog() -> None:
    hints = [b.check_hint for b in BASELINE_ISSUES]
    assert hints.count("missing_primary_key") == 1
    assert hints.count("unindexed_foreign_key") == 12


def test_volume_baseline_prefers_the_same_weekday() -> None:
    from ai_data_engineer.detection.checks.timeseries import volume_baseline

    # Mondays are quiet (22), other days busy (40); one earlier Monday is enough.
    past = [(0, 22), (1, 40), (2, 41), (3, 40), (4, 45), (5, 30), (6, 25)]
    assert volume_baseline(past, weekday=0, min_same_weekday=1) == ([22], "the same weekday")
    values, basis = volume_baseline(past, weekday=0, min_same_weekday=2)
    assert basis == "recent scans"
    assert len(values) == len(past)


def test_volume_baseline_prefers_the_same_weekday() -> None:
    from ai_data_engineer.detection.checks.timeseries import volume_baseline

    # Mondays are quiet (22), other days busy (40); one earlier Monday is enough.
    past = [(0, 22), (1, 40), (2, 41), (3, 40), (4, 45), (5, 30), (6, 25)]
    assert volume_baseline(past, weekday=0, min_same_weekday=1) == ([22], "the same weekday")
    values, basis = volume_baseline(past, weekday=0, min_same_weekday=2)
    assert basis == "recent scans"
    assert len(values) == len(past)
