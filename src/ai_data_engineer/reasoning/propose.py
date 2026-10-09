"""AI proposes business rules; people decide.

1. Build a privacy-safe description of the source (``context.py``: structure and
   statistics, no row values) plus the rules it already has.
2. Ask the LLM for rules in our rule format, each with a confidence and a rationale.
3. Check every proposal deterministically: known tables/columns, comparable types, joins
   only along known relationships. Invalid ones are dropped and reported, never stored.
4. Store the rest as ``proposed`` (origin ``ai``); skip duplicates and anything a person
   already rejected. Nothing runs until someone approves it (``aide rule approve``).
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from ai_data_engineer.discovery.catalog import load_catalog
from ai_data_engineer.graph.models import DataSource, Origin, Rule, RuleStatus, utcnow
from ai_data_engineer.reasoning.context import schema_context
from ai_data_engineer.reasoning.llm import LLMClient
from ai_data_engineer.reasoning.replies import confidence_of, items_of, parse_json_reply
from ai_data_engineer.rules.settings import DEFAULT_RULE_SETTINGS, RuleSettings
from ai_data_engineer.rules.spec import describe, parse_spec, signature, validate
from ai_data_engineer.rules.store import (
    known_relationships,
    rule_with_signature,
    rules_of,
    save_rule,
)

SYSTEM_PROMPT = """\
You are a senior data engineer reviewing a database you have never seen. You get its
structure and column statistics (never row values) as JSON. Propose business rules that
should hold for EVERY row of a healthy database, so that rows breaking them are data
problems worth investigating.

Write each rule in exactly one of these JSON forms:

1. Compare two columns of the same row:
   {"kind": "compare_columns", "table": "billing.refunds", "column": "refunded_at",
    "op": ">=", "other_column": "requested_at"}
2. Compare a column with a column of a parent row, following a listed relationship
   (table = the child side, via.on = [[child column, parent column], ...]):
   {"kind": "compare_columns", "table": "billing.refunds", "column": "refunded_at",
    "op": ">=", "other_column": "charged_at",
    "via": {"parent_table": "billing.charges", "on": [["charge_id", "id"]]}}
3. Compare a numeric column with a constant:
   {"kind": "compare_constant", "table": "billing.refunds", "column": "amount",
    "op": ">", "value": 0}
4. A parent's total equals the sum of its children's amounts (on = [[child column,
   parent column], ...], following a listed relationship):
   {"kind": "sum_matches", "table": "billing.charges", "column": "total",
    "child_table": "billing.charge_items", "child_column": "amount",
    "on": [["charge_id", "id"]], "tolerance": 0.01}

op is one of < <= = >= > <>. Rows where a compared value is NULL are ignored.

Rules:
- Use only tables, columns and relationships listed in the input; joins must follow a
  listed relationship exactly.
- Propose rules a domain expert would expect from the names, types and statistics (dates
  that must come after other dates, amounts and quantities that can't be negative, totals
  that must add up). Use the min/max statistics: don't propose a rule the current data
  already contradicts.
- Go through EVERY listed relationship and compare the child with its parent: a child's
  date usually comes after its parent's (a shipment after its order, a payment after
  its invoice), and a child's amount may equal or be bounded by the parent's (a single
  payment equals its order's total). Propose these with "via".
- Skip rules the database already enforces (primary keys, NOT NULL), rules about id/key
  columns, and anything listed under existing_rules.
- For each rule add "confidence" (0 to 1: how sure you are it should always hold; use
  the whole range: above 0.95 only when it is true by definition, around 0.6-0.8 when
  it depends on how this business works) and "rationale" (one sentence citing the
  names/statistics that suggest it).

Answer with JSON only: {"rules": [ ... ]}
"""


@dataclass
class ProposalResult:
    provider: str
    model: str
    received: int = 0
    proposed: list[Rule] = field(default_factory=list)
    duplicates: int = 0
    previously_rejected: int = 0
    low_confidence: int = 0
    invalid: list[str] = field(default_factory=list)  # "<rule>: <why>"
    input_tokens: int | None = None
    output_tokens: int | None = None

    def summary(self) -> str:
        text = (
            f"{self.provider}/{self.model}: {self.received} rules suggested, "
            f"{len(self.proposed)} new proposals"
        )
        skipped = [
            (self.duplicates, "already known"),
            (self.previously_rejected, "rejected before"),
            (self.low_confidence, "low confidence"),
            (len(self.invalid), "invalid"),
        ]
        details = ", ".join(f"{n} {why}" for n, why in skipped if n)
        return text + (f" (skipped: {details})" if details else "")


def propose_rules(
    session: Session,
    source: DataSource,
    llm: LLMClient,
    *,
    now: datetime | None = None,
    settings: RuleSettings = DEFAULT_RULE_SETTINGS,
) -> ProposalResult:
    now = now or utcnow()
    catalog = load_catalog(session, source)
    relationships = known_relationships(session, catalog)
    links = [link for link, _, _ in relationships]
    context = schema_context(catalog, relationships, settings)
    context["existing_rules"] = [
        {"rule": r.name, "status": r.status.value} for r in rules_of(session, source)
    ]
    prompt = json.dumps(context, indent=1, default=str)

    reply = llm.complete_json(SYSTEM_PROMPT, prompt)
    result = ProposalResult(
        reply.provider, reply.model, input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
    )  # fmt: skip
    items = parse_rules_reply(reply.text)
    result.received = len(items)
    seen: set[str] = set()
    for item in items[: settings.max_proposals]:
        confidence, rationale, raw = _split(item)
        try:
            spec = parse_spec(raw)
        except ValidationError as exc:
            result.invalid.append(f"{_short(raw)}: {exc.errors()[0]['msg']}")
            continue
        problems = validate(spec, catalog, links)
        if problems:
            result.invalid.append(f"{describe(spec)}: {'; '.join(problems)}")
            continue
        sig = signature(spec)
        existing = rule_with_signature(session, source, sig)
        if sig in seen or (existing is not None and existing.status is not RuleStatus.REJECTED):
            result.duplicates += 1
            continue
        if existing is not None:
            result.previously_rejected += 1
            continue
        if confidence is None or confidence < settings.min_confidence:
            result.low_confidence += 1
            continue
        seen.add(sig)
        result.proposed.append(
            save_rule(
                session,
                source=source,
                catalog=catalog,
                spec=spec,
                origin=Origin.AI,
                status=RuleStatus.PROPOSED,
                created_by=f"{reply.provider}:{reply.model}",
                now=now,
                confidence=confidence,
                evidence={
                    "rationale": rationale,
                    "provider": reply.provider,
                    "model": reply.model,
                    "proposed_at": now.isoformat(),
                    # Which input the proposal was based on (the input itself holds no
                    # row values, but isn't stored: it can be rebuilt from the scans).
                    "context_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                },
            )
        )
    return result


def parse_rules_reply(text: str) -> list[dict[str, Any]]:
    """The rule objects in an LLM reply: ``{"rules": [...]}`` or a bare list."""
    return items_of(parse_json_reply(text), "rules")


def _split(item: dict[str, Any]) -> tuple[float | None, str, dict[str, Any]]:
    raw = dict(item)
    confidence = raw.pop("confidence", None)
    rationale = str(raw.pop("rationale", "") or "")[:500]
    for decorative in ("name", "description", "title"):  # models like to add these
        raw.pop(decorative, None)
    return confidence_of(confidence), rationale, raw


def _short(raw: dict[str, Any]) -> str:
    return json.dumps(raw, default=str)[:120]
