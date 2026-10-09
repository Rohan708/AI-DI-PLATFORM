"""Plain-language explanations of findings: what it means, what it affects, likely causes,
what to check next.

The finding itself stays deterministic (``confidence`` NULL); the explanation is extra,
AI-written text stored in ``finding.evidence["explanation"]`` with its provider, model
and confidence.

**Guard: an explanation may only quote numbers that are in its input.** Every number in
the answer is compared with the numbers we sent (counts, rates, thresholds, dates); one
unknown number and the explanation is thrown away, so the model can't invent statistics.

**Privacy:** the input is the finding's own title, description and evidence, the table's
structure and statistics, connected tables (relationships) and other open findings
nearby. Never sent: offending row identifiers (``sample_rows``), and the text values in
category findings (those are reduced to counts).
"""

import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.detection.recording import SYSTEM_ACTOR
from ai_data_engineer.discovery.catalog import Catalog, TableInfo, load_catalog
from ai_data_engineer.graph.models import (
    ACTIVE_FINDING_STATUSES,
    DataSource,
    Finding,
    Severity,
    utcnow,
)
from ai_data_engineer.reasoning.context import RANGE_FAMILIES
from ai_data_engineer.reasoning.llm import LLMClient
from ai_data_engineer.reasoning.replies import confidence_of, parse_json_reply
from ai_data_engineer.rules.store import known_relationships

# Evidence keys never sent: identifiers of offending rows.
PRIVATE_EVIDENCE_KEYS = frozenset({"sample_rows", "explanation"})
# Checks whose title/description/evidence contain text values from the data.
TEXT_VALUE_CHECKS = frozenset({"inconsistent_categories"})
# Numbers an explanation may use without them being in the input ("one table", "100%").
FREE_NUMBERS = frozenset({0.0, 1.0, 2.0, 100.0})
# "about 41" for 40.6 is fine: relative tolerance when matching a quoted number.
NUMBER_TOLERANCE = 0.02
MAX_NEARBY_FINDINGS = 10

_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_SEVERITY_ORDER = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO]

SYSTEM_PROMPT = """\
You explain data-quality findings to the people who own a database: engineers and
analysts who know their business but not this tool. You get one finding (what was
detected, with its numbers and evidence), the table it is on, the tables connected to it,
and other open findings nearby. You never see row values.

Write for a busy reader:
- "summary": 1-2 sentences, what is wrong, in plain words.
- "impact": 1-2 sentences, what this can break: reports, joins, connected tables listed in
  the input, downstream totals.
- "likely_causes": 1-3 short, specific guesses (e.g. "a deploy changed the signup form",
  "the nightly load ran only partly"). Mention other open findings if they suggest a
  common cause.
- "next_steps": 1-3 concrete checks a person can do.
- "confidence": 0.0-1.0, how sure you are of the causes.

Use ONLY numbers that appear in the input; do not calculate new numbers or invent
statistics. Don't repeat the whole description. Answer with JSON only:
{"summary": "...", "impact": "...", "likely_causes": ["..."], "next_steps": ["..."],
 "confidence": 0.0}
"""


@dataclass
class ExplainResult:
    provider: str
    model: str
    explained: int = 0
    rejected: list[str] = field(default_factory=list)  # "<finding>: <why>"

    def summary(self) -> str:
        text = f"{self.provider}/{self.model}: {self.explained} findings explained"
        return text + (f", {len(self.rejected)} answers rejected" if self.rejected else "")


def explain_findings(
    session: Session,
    source: DataSource,
    llm: LLMClient,
    findings: Iterable[Finding],
    *,
    now: datetime | None = None,
) -> ExplainResult:
    """Explain each finding with one LLM call; keep only answers that pass the guard."""
    now = now or utcnow()
    catalog = load_catalog(session, source)
    tables = {t.asset_key: t for t in catalog.tables.values()}
    result = ExplainResult(llm.provider, llm.model)
    for finding in findings:
        context = finding_context(session, catalog, tables, finding)
        reply = llm.complete_json(SYSTEM_PROMPT, json.dumps(context, indent=1, default=str))
        result.provider, result.model = reply.provider, reply.model
        explanation, problem = check_explanation(parse_json_reply(reply.text), context)
        if explanation is None:
            result.rejected.append(f"{str(finding.id)[:8]}: {problem}")
            continue
        finding.evidence = {
            **finding.evidence,
            "explanation": {
                **explanation,
                "provider": reply.provider,
                "model": reply.model,
                "at": now.isoformat(),
                "explained_title": finding.title,
                "by": SYSTEM_ACTOR,
            },
        }
        result.explained += 1
    session.flush()
    return result


def unexplained(session: Session, source: DataSource, limit: int) -> list[Finding]:
    """Open findings without an up-to-date explanation, worst first."""
    found = session.scalars(
        select(Finding).where(
            Finding.data_source_id == source.id, Finding.status.in_(ACTIVE_FINDING_STATUSES)
        )
    ).all()
    todo = [f for f in found if not is_current(f)]
    todo.sort(key=lambda f: (_SEVERITY_ORDER.index(f.severity), f.first_detected_at))
    return todo[:limit]


def is_current(finding: Finding) -> bool:
    """Has an explanation of the finding as it reads now (titles quote the numbers)."""
    explanation = finding.evidence.get("explanation")
    return bool(explanation and explanation.get("explained_title") == finding.title)


def finding_context(
    session: Session, catalog: Catalog, tables: dict[uuid.UUID, TableInfo], finding: Finding
) -> dict[str, Any]:
    table = tables.get(finding.asset_key) if finding.asset_key else None
    context: dict[str, Any] = {"finding": shareable_finding(finding)}
    if table is None:
        return context
    context["table"] = _table(table, finding)
    connected: set[str] = set()
    links = []
    for (child, child_cols, parent, parent_cols), status, _ in known_relationships(
        session, catalog
    ):
        if table.ref not in (child, parent):
            continue
        points_out = child == table.ref
        connected.add(parent if points_out else child)
        links.append(
            {
                "link": f"{child}({', '.join(child_cols)}) -> {parent}({', '.join(parent_cols)})",
                "status": status,
                "direction": "outgoing" if points_out else "incoming (depends on this table)",
            }
        )
    context["connected_tables"] = links
    nearby_keys = {t.asset_key for t in catalog.tables.values() if t.ref in connected}
    nearby_keys.add(table.asset_key)
    nearby = session.scalars(
        select(Finding).where(
            Finding.data_source_id == finding.data_source_id,
            Finding.status.in_(ACTIVE_FINDING_STATUSES),
            Finding.asset_key.in_(nearby_keys),
            Finding.id != finding.id,
        )
    ).all()
    nearby = sorted(nearby, key=lambda f: _SEVERITY_ORDER.index(f.severity))
    context["other_open_findings_nearby"] = [
        {"check": f.check_name, "severity": f.severity.value, "title": _title(f)}
        for f in nearby[:MAX_NEARBY_FINDINGS]
    ]
    return context


def check_explanation(
    data: Any, context: dict[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    """(explanation, "") if usable, else (None, why)."""
    if not isinstance(data, dict):
        return None, "not a JSON object"
    summary, impact = str(data.get("summary") or ""), str(data.get("impact") or "")
    if not summary.strip():
        return None, "no summary"
    causes = [str(c) for c in data.get("likely_causes") or [] if str(c).strip()][:3]
    steps = [str(s) for s in data.get("next_steps") or [] if str(s).strip()][:3]
    explanation = {
        "summary": summary[:600],
        "impact": impact[:600],
        "likely_causes": [c[:300] for c in causes],
        "next_steps": [s[:300] for s in steps],
        "confidence": confidence_of(data.get("confidence")),
    }
    text = " ".join([summary, impact, *causes, *steps])
    unknown = unsupported_numbers(text, json.dumps(context, default=str))
    if unknown:
        return None, f"quotes numbers not in the evidence: {', '.join(unknown[:5])}"
    return explanation, ""


def unsupported_numbers(text: str, source: str) -> list[str]:
    """Numbers in ``text`` that don't match a number in ``source`` (within tolerance; a
    fraction may be quoted as a percentage)."""
    known: set[float] = set(FREE_NUMBERS)
    for token in _NUMBER.findall(source):
        value = _value(token)
        if value is not None:
            known.add(value)
            if -1 <= value <= 1:
                known.add(round(value * 100, 6))
    unknown = []
    for token in _NUMBER.findall(text):
        value = _value(token)
        if value is None or any(_close(value, k) for k in known):
            continue
        unknown.append(token)
    return unknown


def _close(value: float, known: float) -> bool:
    return abs(value - known) <= NUMBER_TOLERANCE * max(1.0, abs(known))


def _value(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def shareable_finding(finding: Finding) -> dict[str, Any]:
    if finding.check_name in TEXT_VALUE_CHECKS:
        evidence = {
            "known_values_count": len(finding.evidence.get("known_values", [])),
            "new_values_count": len(finding.evidence.get("new_values", [])),
            "history_scans": finding.evidence.get("history_scans"),
            "note": "the values themselves are not shared",
        }
        description = None
    else:
        evidence = {k: v for k, v in finding.evidence.items() if k not in PRIVATE_EVIDENCE_KEYS}
        description = finding.description
    return {
        "check": finding.check_name,
        "category": finding.category.value,
        "severity": finding.severity.value,
        "status": finding.status.value,
        "title": _title(finding),
        "description": description,
        "first_detected": finding.first_detected_at.date().isoformat(),
        "last_detected": finding.last_detected_at.date().isoformat(),
        "evidence": evidence,
    }


def _title(finding: Finding) -> str:
    if finding.check_name in TEXT_VALUE_CHECKS:
        return f"inconsistent category values in a column ({finding.check_name})"
    return finding.title


def _table(table: TableInfo, finding: Finding) -> dict[str, Any]:
    out: dict[str, Any] = {
        "table": table.ref,
        "rows": table.estimated_rows,
        "primary_key": list(table.primary_key),
        "columns": [c.name for c in table.columns.values()],
    }
    column = next(
        (c for c in table.columns.values() if c.column_key == finding.column_key), None
    )
    if column is not None:
        out["column"] = {
            "name": column.name,
            "type": column.native_type,
            "nulls": column.null_count,
            "distinct": column.distinct_count,
        }
        if column.family in RANGE_FAMILIES:
            out["column"]["min"], out["column"]["max"] = column.min_repr, column.max_repr
    return out
