"""Stage 2.3-2.4 against the real lab, with a fake LLM: AI opinions on unsure
relationships (advice only, kept across nightly refreshes) and finding explanations
(guarded, private, kept across refreshes)."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.db import create_db_engine
from ai_data_engineer.detection.recording import record_finding
from ai_data_engineer.detection.runner import detect
from ai_data_engineer.discovery.catalog import load_catalog
from ai_data_engineer.discovery.discover import discover
from ai_data_engineer.discovery.store import current_relationships
from ai_data_engineer.graph.models import (
    DataSource,
    Finding,
    FindingCategory,
    RelationshipKind,
    RelationshipStatus,
    Severity,
)
from ai_data_engineer.ingestion.postgres import PostgresAdapter
from ai_data_engineer.ingestion.scan import scan_source
from ai_data_engineer.ingestion.settings import ScanSettings
from ai_data_engineer.lab.runner import build_lab
from ai_data_engineer.reasoning.explain import explain_findings, is_current, unexplained
from ai_data_engineer.reasoning.llm import LLMReply
from ai_data_engineer.reasoning.review_relationships import review_relationships

SETTINGS = ScanSettings(exclude_schemas=("aide_lab",))
T1 = datetime(2026, 8, 1, 3, tzinfo=UTC)


class ScriptedLLM:
    """Answers with the given replies in turn and records every prompt."""

    provider = "fake"
    model = "fake-1"

    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = [json.dumps(r) for r in replies]
        self.prompts: list[str] = []

    def complete_json(self, system: str, prompt: str) -> LLMReply:
        self.prompts.append(prompt)
        text = self.replies.pop(0) if self.replies else '{"reviews": []}'
        return LLMReply(text, self.provider, self.model)


def _factory(url: str) -> Callable[[DataSource], Any]:
    return lambda _source: PostgresAdapter(url, SETTINGS)


def _discovered(session: Session, source: DataSource, make_database: Callable[[], str]) -> str:
    url = make_database()
    lab = create_db_engine(url)
    build_lab(lab, seed=61, size="tiny")
    lab.dispose()
    scan_source(session, source, observed_at=T1, adapter_factory=_factory(url))
    discover(session, source, adapter_factory=_factory(url))
    return url


def test_ai_opinions_on_unsure_relationships_are_advice_only(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url = _discovered(session, source, make_database)
    catalog = load_catalog(session, source)
    inferred = [
        r for r in current_relationships(session, catalog) if r.kind is RelationshipKind.INFERRED
    ]
    unsure = inferred[0]
    unsure.confidence = 0.7  # pretend discovery wasn't sure about this one
    for other in inferred[1:]:
        other.confidence = 0.95
    session.flush()

    llm = ScriptedLLM(
        {"reviews": [
            {"id": "1", "verdict": "likely", "confidence": 0.8, "reason": "names and overlap fit"},
            {"id": "99", "verdict": "likely", "confidence": 0.9},  # not something we asked
        ]}
    )  # fmt: skip
    result = review_relationships(session, source, llm, now=T1)

    assert (result.asked, result.reviewed, result.unusable) == (1, 1, 1)
    review = unsure.evidence["ai_review"]
    assert (review["verdict"], review["confidence"]) == ("likely", 0.8)
    assert unsure.status is RelationshipStatus.PROPOSED  # a person still decides
    asked = json.loads(llm.prompts[0])["links"][0]
    assert {"child", "parent", "evidence", "tool_confidence"} <= set(asked)

    # The nightly refresh keeps the opinion (with the evidence it was based on).
    discover(session, source, adapter_factory=_factory(url))
    assert unsure.evidence["ai_review"] == review
    assert unsure.evidence["ai_review"]["evidence_sha256"]


def test_explanations_are_guarded_private_and_kept(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    _discovered(session, source, make_database)
    detect(session, source)
    no_pk = session.scalars(
        select(Finding).where(Finding.check_name == "missing_primary_key")
    ).one()
    orphan_like, _ = record_finding(
        session, source=source, fingerprint="orphans-test", category=FindingCategory.RELATIONAL,
        check_name="orphan_rows", severity=Severity.HIGH,
        title="56 rows in shop.orders point to missing shop.customers rows", description="d",
        evidence={"orphan_rows": 56, "sample_rows": [{"order_id": "77123456"}]}, now=T1,
        asset_key=no_pk.asset_key,
    )  # fmt: skip
    assert no_pk in unexplained(session, source, limit=50)

    llm = ScriptedLLM(
        {
            "summary": "legacy.INV_LINE_TAX has no primary key, so duplicate tax lines slip in.",
            "impact": "Tax totals per invoice line can be counted twice.",
            "likely_causes": ["the legacy schema never defined one"],
            "next_steps": ["check for duplicate (INV_NO, LINE_NO) pairs"],
            "confidence": 0.7,
        },
        {"summary": "About 4,200 orders a week are affected.", "impact": "", "confidence": 0.5},
    )
    result = explain_findings(session, source, llm, [no_pk, orphan_like], now=T1)

    assert result.explained == 1
    assert "4,200" in result.rejected[0]  # an invented number: explanation thrown away
    assert is_current(no_pk)
    assert "explanation" not in orphan_like.evidence
    assert no_pk.confidence is None  # the finding itself stays deterministic
    assert "77123456" not in llm.prompts[1]  # offending row ids are never sent
    assert "sample_rows" not in llm.prompts[1]
    context = json.loads(llm.prompts[0])
    assert context["table"]["table"] == "legacy.INV_LINE_TAX"
    assert "connected_tables" in context

    detect(session, source)  # the nightly refresh keeps the explanation
    assert no_pk.evidence["explanation"]["summary"].startswith("legacy.INV_LINE_TAX")
    assert no_pk not in unexplained(session, source, limit=50)
