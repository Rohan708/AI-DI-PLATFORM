"""AI relationship review and finding explanations: the parts that need no database
(the number guard, privacy redaction, when a review is due). No real LLM calls."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from ai_data_engineer.discovery.settings import DiscoverySettings
from ai_data_engineer.graph.models import (
    Finding,
    FindingCategory,
    FindingStatus,
    Relationship,
    RelationshipKind,
    RelationshipStatus,
    Severity,
)
from ai_data_engineer.reasoning.explain import (
    check_explanation,
    shareable_finding,
    unsupported_numbers,
)
from ai_data_engineer.reasoning.replies import confidence_of
from ai_data_engineer.reasoning.review_relationships import evidence_hash, needs_review

T = datetime(2026, 3, 16, 3, tzinfo=UTC)
CONTEXT = {
    "finding": {
        "title": "NULLs in shop.addresses.postal_code jumped from 0.0% to 8.0%",
        "evidence": {"baseline_null_rate": 0.0, "current_null_rate": 0.08, "null_rows": 154,
                     "rows": 1925, "usual": 40.6, "first_detected": "2026-03-15"},
    }
}  # fmt: skip


# --- the number guard ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "unknown"),
    [
        ("NULLs went from 0% to 8% (154 of 1,925 rows)", []),  # fractions as percentages
        ("about 41 rows a day", []),  # 40.6, rounded
        ("since 2026-03-15", []),  # dates from the input
        ("one table, 100% of rows", []),
        ("that is 26 rows fewer", ["26"]),  # a calculated number
        ("roughly 3,000 customers affected", ["3,000"]),  # an invented one
    ],
)
def test_explanations_may_only_quote_numbers_from_the_input(text: str, unknown: list[str]) -> None:
    assert unsupported_numbers(text, json.dumps(CONTEXT)) == unknown


def test_check_explanation() -> None:
    good = {
        "summary": "8% of postal codes are suddenly missing.",
        "impact": "Shipping labels for 154 addresses can't be printed.",
        "likely_causes": ["a form change made the field optional"],
        "next_steps": ["check yesterday's deploy"],
        "confidence": 0.6,
    }
    explanation, problem = check_explanation(good, CONTEXT)
    assert problem == ""
    assert explanation is not None
    assert explanation["confidence"] == 0.6

    invented = {**good, "impact": "About 2,300 orders a week will fail to ship."}
    assert check_explanation(invented, CONTEXT)[1].startswith("quotes numbers not in the evidence")
    assert check_explanation({"impact": "x"}, CONTEXT) == (None, "no summary")
    assert check_explanation(["not", "an", "object"], CONTEXT) == (None, "not a JSON object")


# --- privacy ---------------------------------------------------------------------------------


def _finding(check: str, title: str, evidence: dict[str, Any]) -> Finding:
    return Finding(
        id=uuid.uuid4(), check_name=check, category=FindingCategory.COLUMN_VALUE,
        severity=Severity.MEDIUM, status=FindingStatus.OPEN, title=title,
        description=title, evidence=evidence, first_detected_at=T, last_detected_at=T,
    )  # fmt: skip


def test_row_ids_and_category_values_are_never_shared() -> None:
    orphans = _finding(
        "orphan_rows", "56 rows in shop.orders point to missing shop.customers rows",
        {"orphan_rows": 56, "sample_rows": [{"order_id": "981234"}]},
    )  # fmt: skip
    shared = json.dumps(shareable_finding(orphans))
    assert "981234" not in shared
    assert "56" in shared

    variants = _finding(
        "inconsistent_categories", "'Deutschland' (12 rows) looks like a variant of 'DE'",
        {"known_values": ["DE", "US"], "new_values": ["Deutschland"], "history_scans": 14},
    )  # fmt: skip
    shared = json.dumps(shareable_finding(variants))
    assert "Deutschland" not in shared
    assert '"DE"' not in shared
    assert '"new_values_count": 1' in shared


# --- when a relationship needs an AI review ---------------------------------------------------


def _rel(**kw: Any) -> Relationship:
    fields: dict[str, Any] = {
        "kind": RelationshipKind.INFERRED, "status": RelationshipStatus.PROPOSED,
        "confidence": 0.7, "evidence": {"inclusion": 0.98, "query_log_calls": 0},
    }  # fmt: skip
    return Relationship(**{**fields, **kw})


def test_which_relationships_get_a_second_opinion() -> None:
    s = DiscoverySettings()
    assert needs_review(_rel(), s)
    assert not needs_review(_rel(confidence=0.95), s)  # sure enough to run orphan checks
    assert not needs_review(_rel(status=RelationshipStatus.CONFIRMED), s)  # a person decided
    assert not needs_review(_rel(kind=RelationshipKind.DECLARED, confidence=None), s)

    reviewed = _rel()
    review = {"evidence_sha256": evidence_hash(reviewed)}
    reviewed.evidence = {**reviewed.evidence, "ai_review": review}
    assert not needs_review(reviewed, s)  # already reviewed on this evidence
    assert needs_review(reviewed, s, again=True)
    reviewed.evidence = {**reviewed.evidence, "inclusion": 0.91}  # new measurements
    assert needs_review(reviewed, s)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.7, 0.7), ("0.4", 0.4), (1.5, None), (-0.1, None), ("high", None), (None, None)],
)
def test_confidence_of(value: object, expected: float | None) -> None:
    assert confidence_of(value) == expected
