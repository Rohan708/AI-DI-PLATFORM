"""Benchmark scoring, answer-key format, and the scenario/relationship catalog."""

from datetime import date

import pytest

from ai_data_engineer.graph.models import FindingCategory
from ai_data_engineer.lab.answer_key import AnswerKey, ExpectedAnomaly
from ai_data_engineer.lab.runner import PLANS, STAGE_ONE_SCENARIOS
from ai_data_engineer.lab.scenarios import SCENARIOS
from ai_data_engineer.lab.schema import TRUE_RELATIONSHIPS, ExpectedRelationship
from ai_data_engineer.lab.scorer import FoundFinding, FoundRelationship, matches, score
from ai_data_engineer.lab.state import LabState

DAY = date(2026, 2, 1)


def _anomaly(
    scenario: str,
    category: FindingCategory,
    table: str,
    column: str | None,
    related: tuple[str, ...] = (),
) -> ExpectedAnomaly:
    return ExpectedAnomaly(
        scenario=scenario,
        category=category,
        table=table,
        column=column,
        check_hint="x",
        detect_stage="1.5",
        description="d",
        effective_day=DAY,
        related_tables=related,
    )


def _finding(category: FindingCategory, table: str | None, column: str | None) -> FoundFinding:
    return FoundFinding(category, table, column, check_name="check", title="t")


NULL_SPIKE = _anomaly("null_spike", FindingCategory.COLUMN_VALUE, "shop.addresses", "postal_code")
HALF_LOAD = _anomaly(
    "half_load", FindingCategory.TIME_SERIES, "shop.orders", None, related=("shop.payments",)
)
ORDERS_TO_CUSTOMERS = ExpectedRelationship(
    "shop.orders", ("cust_no",), "shop.customers", ("id",), declared=False
)
COMPOSITE = ExpectedRelationship(
    "legacy.INV_LINE_TAX",
    ("INV_NO", "LINE_NO"),
    "legacy.INV_LINE",
    ("INV_NO", "LINE_NO"),
    declared=False,
)


def _key(
    *anomalies: ExpectedAnomaly, relationships: tuple[ExpectedRelationship, ...] = ()
) -> AnswerKey:
    return AnswerKey(
        seed=1, size="tiny", current_day=DAY, anomalies=anomalies, relationships=relationships
    )


# --- matching ---------------------------------------------------------------------------


def test_match_requires_same_category_and_table() -> None:
    assert matches(
        NULL_SPIKE, _finding(FindingCategory.COLUMN_VALUE, "shop.addresses", "postal_code")
    )
    assert not matches(
        NULL_SPIKE, _finding(FindingCategory.STRUCTURAL, "shop.addresses", "postal_code")
    )
    assert not matches(
        NULL_SPIKE, _finding(FindingCategory.COLUMN_VALUE, "shop.orders", "postal_code")
    )
    assert not matches(NULL_SPIKE, _finding(FindingCategory.COLUMN_VALUE, "shop.addresses", "city"))


def test_table_level_findings_match_column_anomalies_and_vice_versa() -> None:
    assert matches(NULL_SPIKE, _finding(FindingCategory.COLUMN_VALUE, "shop.addresses", None))
    assert matches(HALF_LOAD, _finding(FindingCategory.TIME_SERIES, "shop.orders", "order_date"))


# --- scoring ----------------------------------------------------------------------------


def test_score_counts_caught_missed_related_and_false_alarms() -> None:
    findings = [
        _finding(FindingCategory.COLUMN_VALUE, "shop.addresses", "postal_code"),  # caught
        _finding(FindingCategory.TIME_SERIES, "shop.payments", None),  # related to half_load
        _finding(FindingCategory.COLUMN_VALUE, "shop.customers", "middle_name"),  # false alarm
    ]
    report = score(_key(NULL_SPIKE, HALF_LOAD), findings, [])

    assert [a.scenario for a, _ in report.caught] == ["null_spike"]
    assert [a.scenario for a in report.missed] == ["half_load"]
    assert len(report.related) == 1
    assert len(report.false_alarms) == 1
    assert report.recall == 0.5
    assert report.precision == 0.5
    assert report.by_category[FindingCategory.COLUMN_VALUE].false_alarms == 1


def test_empty_result_scores_zero_recall_and_no_precision() -> None:
    report = score(_key(NULL_SPIKE), [], [])
    assert report.recall == 0.0
    assert report.precision is None
    assert "0 / 1" in report.to_markdown()


def test_relationship_scoring_is_column_order_insensitive_for_composites() -> None:
    found = [
        FoundRelationship(
            "legacy.INV_LINE_TAX", ("LINE_NO", "INV_NO"), "legacy.INV_LINE", ("LINE_NO", "INV_NO")
        ),
        FoundRelationship("shop.orders", ("ship_addr",), "shop.customers", ("id",)),  # wrong
    ]
    report = score(_key(relationships=(ORDERS_TO_CUSTOMERS, COMPOSITE)), [], found)

    assert report.relationships_found == [COMPOSITE]
    assert report.relationships_missed == [ORDERS_TO_CUSTOMERS]
    assert len(report.relationships_false) == 1
    assert report.relationship_recall == 0.5
    assert report.relationship_precision == 0.5


# --- answer key and catalog -------------------------------------------------------------


def test_expected_anomaly_json_round_trip() -> None:
    assert ExpectedAnomaly.from_json(HALF_LOAD.to_json()) == HALF_LOAD


def test_answer_key_markdown_lists_everything() -> None:
    markdown = _key(NULL_SPIKE, relationships=TRUE_RELATIONSHIPS).to_markdown()
    assert "null_spike" in markdown
    assert "15; 11 hidden" in markdown
    assert "must NOT be flagged" in markdown


def test_relationship_catalog() -> None:
    assert sum(r.declared for r in TRUE_RELATIONSHIPS) == 4
    assert sum(not r.declared for r in TRUE_RELATIONSHIPS) == 11
    for r in TRUE_RELATIONSHIPS:
        assert len(r.from_columns) == len(r.to_columns)


def test_scenario_catalog_and_plans() -> None:
    assert len(SCENARIOS) == 13
    assert "ship_before_order" not in STAGE_ONE_SCENARIOS
    assert "fat_finger_quantity" not in STAGE_ONE_SCENARIOS
    covered = {s.category for s in SCENARIOS.values()}
    assert covered == {
        FindingCategory.STRUCTURAL,
        FindingCategory.RELATIONAL,
        FindingCategory.COLUMN_VALUE,
        FindingCategory.TIME_SERIES,
        FindingCategory.BUSINESS_RULE,
        FindingCategory.ROW_OUTLIER,
    }
    for plan in PLANS.values():
        assert set(plan.scenarios) <= set(SCENARIOS)


def test_lab_state_round_trip_and_id_counters() -> None:
    state = LabState(seed=3, size="tiny", start_date=DAY, current_day=DAY)
    assert state.take("order") == 1
    assert state.take("order") == 2
    restored = LabState.from_json(state.to_json())
    assert restored == state
    with pytest.raises(KeyError):
        state.take("nope")
