"""Stage 2 against the real lab, with a fake LLM (no real AI calls in tests): proposals are
validated and stored for review, approved rules run as plain SQL, catch the two planted
business-rule problems, and decisions stick."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ai_data_engineer.db import create_db_engine
from ai_data_engineer.discovery.discover import discover
from ai_data_engineer.graph.models import (
    DataSource,
    Finding,
    FindingCategory,
    Origin,
    Rule,
    RuleStatus,
)
from ai_data_engineer.ingestion.postgres import PostgresAdapter
from ai_data_engineer.ingestion.scan import scan_source
from ai_data_engineer.ingestion.settings import ScanSettings
from ai_data_engineer.lab.answer_key import AnswerKey
from ai_data_engineer.lab.rule_scoring import load_ai_rules, score_rules
from ai_data_engineer.lab.runner import build_lab, inject
from ai_data_engineer.lab.schema import HIDDEN_RULES
from ai_data_engineer.lab.scorer import load_findings, score
from ai_data_engineer.reasoning.llm import LLMReply
from ai_data_engineer.reasoning.propose import propose_rules
from ai_data_engineer.rules.checks import check_rules
from ai_data_engineer.rules.store import review_rule, rule_spec

SETTINGS = ScanSettings(exclude_schemas=("aide_lab",))
T1 = datetime(2026, 7, 1, 3, tzinfo=UTC)

SHIPPED_AFTER_ORDER = {
    "kind": "compare_columns", "table": "shop.shipments", "column": "shipped_at", "op": ">=",
    "other_column": "order_date",
    "via": {"parent_table": "shop.orders", "on": [["ord_id", "order_id"]]},
    "confidence": 0.9, "rationale": "a parcel can't ship before it was ordered",
}  # fmt: skip
INVOICE_TOTAL = {
    "kind": "sum_matches", "table": "legacy.INV_HDR", "column": "TOTAL_AMT",
    "child_table": "legacy.INV_LINE", "child_column": "LINE_AMT",
    "on": [["INV_NO", "INV_NO"]], "tolerance": 0.01,
    "confidence": 0.85, "rationale": "header totals usually add up their lines",
}  # fmt: skip
UNKNOWN_COLUMN = {
    "kind": "compare_constant", "table": "shop.orders", "column": "discount", "op": ">=",
    "value": 0, "confidence": 0.8,
}  # fmt: skip
UNKNOWN_JOIN = {  # no relationship links shipments.ord_id to customers
    "kind": "compare_columns", "table": "shop.shipments", "column": "shipped_at", "op": ">=",
    "other_column": "created_at",
    "via": {"parent_table": "shop.customers", "on": [["ord_id", "id"]]},
    "confidence": 0.7,
}  # fmt: skip
UNSURE = {
    "kind": "compare_constant", "table": "shop.products", "column": "weight_grams", "op": ">",
    "value": 0, "confidence": 0.2,
}  # fmt: skip


class FakeLLM:
    provider = "fake"
    model = "fake-1"

    def __init__(self, rules: list[dict[str, Any]]) -> None:
        self.text = json.dumps({"rules": rules})
        self.prompts: list[str] = []

    def complete_json(self, system: str, prompt: str) -> LLMReply:
        self.prompts.append(prompt)
        return LLMReply(self.text, self.provider, self.model)


def _factory(url: str) -> Callable[[DataSource], Any]:
    return lambda _source: PostgresAdapter(url, SETTINGS)


def _discovered_lab(
    session: Session, source: DataSource, make_database: Callable[[], str], seed: int
) -> tuple[str, Engine]:
    url = make_database()
    lab = create_db_engine(url)
    build_lab(lab, seed=seed, size="tiny")
    scan_source(session, source, observed_at=T1, adapter_factory=_factory(url))
    discover(session, source, adapter_factory=_factory(url))
    return url, lab


def test_ai_proposals_are_checked_reviewed_and_catch_planted_problems(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url, lab = _discovered_lab(session, source, make_database, seed=51)
    llm = FakeLLM([SHIPPED_AFTER_ORDER, INVOICE_TOTAL, UNKNOWN_COLUMN, UNKNOWN_JOIN, UNSURE,
                   SHIPPED_AFTER_ORDER])  # fmt: skip

    result = propose_rules(session, source, llm, now=T1)

    assert len(result.proposed) == 2, result.invalid
    assert result.duplicates == 1
    assert result.low_confidence == 1
    assert len(result.invalid) == 2
    assert any("unknown column shop.orders.discount" in p for p in result.invalid)
    assert any("no known relationship" in p for p in result.invalid)
    rules = session.scalars(select(Rule).where(Rule.data_source_id == source.id)).all()
    assert {(r.origin, r.status) for r in rules} == {(Origin.AI, RuleStatus.PROPOSED)}
    assert all(r.confidence is not None and r.evidence["rationale"] for r in rules)

    # Privacy: the prompt carries structure and statistics, no text values.
    prompt = json.loads(llm.prompts[0])
    customers = next(t for t in prompt["tables"] if t["table"] == "shop.customers")
    email = next(c for c in customers["columns"] if c["name"] == "email")
    assert "min" not in email
    assert "max" not in email
    assert "@" not in llm.prompts[0]

    # Proposed rules don't run.
    assert check_rules(session, source, adapter_factory=_factory(url)).checked == 0

    for rule in rules:
        review_rule(session, rule, RuleStatus.ACTIVE, actor="alice", now=T1)
    clean = check_rules(session, source, adapter_factory=_factory(url))
    assert (clean.checked, clean.opened, clean.not_rechecked) == (2, 0, 0), clean.errors

    planted = inject(lab, ["ship_before_order", "invoice_total_mismatch"])
    lab.dispose()
    broken = check_rules(session, source, adapter_factory=_factory(url))
    assert broken.opened == 2, broken.errors

    findings = session.scalars(
        select(Finding).where(Finding.category == FindingCategory.BUSINESS_RULE)
    ).all()
    table_of = {r.id: rule_spec(r).table for r in rules}
    by_table = {table_of[f.rule_id]: f for f in findings if f.rule_id is not None}
    assert by_table["shop.shipments"].evidence["violating_rows"] == planted[0].details["rows"]
    assert by_table["legacy.INV_HDR"].evidence["violating_rows"] == planted[1].details["rows"]
    assert all(f.confidence is None and f.rule_id is not None for f in findings)
    assert by_table["shop.shipments"].evidence["sample_rows"]  # offending shipment ids

    key = AnswerKey(seed=51, size="tiny", current_day=T1.date(), anomalies=tuple(planted),
                    relationships=(), baseline_issues=())  # fmt: skip
    report = score(key, load_findings(session, source.id), [])
    assert [a.scenario for a, _ in report.caught] == [
        "ship_before_order",
        "invoice_total_mismatch",
    ]

    coverage = score_rules(HIDDEN_RULES, load_ai_rules(session, source.id))
    assert len(coverage.found) == 2
    assert coverage.approved == 2


def test_rejected_proposals_are_not_proposed_again(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    _discovered_lab(session, source, make_database, seed=52)
    llm = FakeLLM([SHIPPED_AFTER_ORDER])
    (rule,) = propose_rules(session, source, llm, now=T1).proposed
    review_rule(session, rule, RuleStatus.REJECTED, actor="bob", now=T1, note="we pre-ship")

    again = propose_rules(session, source, llm, now=T1)

    assert again.proposed == []
    assert again.previously_rejected == 1
    assert rule.evidence["review_history"][-1]["note"] == "we pre-ship"
    assert '"rejected"' in llm.prompts[-1]  # the model is told what was rejected


def test_without_active_rules_nothing_connects(session: Session, source: DataSource) -> None:
    def no_connection(_source: DataSource) -> Any:
        raise AssertionError("should not connect")

    assert check_rules(session, source, adapter_factory=no_connection).checked == 0
