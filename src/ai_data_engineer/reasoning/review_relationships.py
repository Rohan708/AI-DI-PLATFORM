"""AI second opinion on relationships discovery is unsure about.

Discovery scores links from names, value overlap and the query log. Below the
orphan-check threshold (0.9) a link is "probably, but not certainly" real, and a
person has to decide. The AI looks at the same evidence a person would (names, types,
counts, overlap, query-log joins; never row values) and says *likely*, *unlikely* or
*unsure*, with a confidence and a reason.

The opinion is advice only: it's stored in ``relationship.evidence["ai_review"]`` and
sorts the review list. The status changes only when a person confirms or rejects.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from ai_data_engineer.discovery.catalog import ColumnInfo, load_catalog
from ai_data_engineer.discovery.settings import DEFAULT_DISCOVERY_SETTINGS, DiscoverySettings
from ai_data_engineer.discovery.store import current_relationships
from ai_data_engineer.graph.models import (
    DataSource,
    Relationship,
    RelationshipKind,
    RelationshipStatus,
    utcnow,
)
from ai_data_engineer.reasoning.llm import LLMClient
from ai_data_engineer.reasoning.replies import confidence_of, items_of, parse_json_reply

Verdict = Literal["likely", "unlikely", "unsure"]
VERDICTS: tuple[Verdict, ...] = ("likely", "unlikely", "unsure")

SYSTEM_PROMPT = """\
You are a senior data engineer helping to document an undocumented database. An
automatic tool proposed links between tables (like undeclared foreign keys) but is not
sure about them. For each proposed link you get the column names and types on both sides,
row and distinct counts, and the tool's evidence: the share of child values found in the
parent ("inclusion"), how often the application's own queries join these columns
("query_log_calls"), a name-similarity score, and its reasons. You never see row values.

For each link, judge whether the child column really refers to the parent's rows:
- "likely": names, types and evidence fit a real reference.
- "unlikely": probably a coincidence, e.g. small sequential numbers that overlap by
  chance, a count or measure that happens to fit, or a link between unrelated concepts.
- "unsure": the evidence could go either way.

Answer with JSON only:
{"reviews": [{"id": "<id from the input>", "verdict": "likely|unlikely|unsure",
  "confidence": 0.0-1.0, "reason": "one sentence citing the names or evidence"}]}
"""


@dataclass
class ReviewResult:
    provider: str
    model: str
    asked: int = 0
    reviewed: int = 0
    by_verdict: dict[str, int] = field(default_factory=dict)
    unusable: int = 0  # answers we couldn't use (unknown id, bad verdict/confidence)

    def summary(self) -> str:
        if not self.asked:
            return "no unsure relationships to review"
        verdicts = ", ".join(f"{n} {v}" for v, n in sorted(self.by_verdict.items()))
        text = f"{self.provider}/{self.model}: {self.reviewed} of {self.asked} reviewed"
        text += f" ({verdicts})" if verdicts else ""
        return text + (f", {self.unusable} answers unusable" if self.unusable else "")


def needs_review(rel: Relationship, settings: DiscoverySettings, again: bool = False) -> bool:
    """Inferred, not decided by a person, below the orphan-check threshold, and (unless
    ``again``) not already reviewed on the same evidence."""
    if rel.kind is not RelationshipKind.INFERRED or rel.status is not RelationshipStatus.PROPOSED:
        return False
    if rel.confidence is not None and rel.confidence >= settings.orphan_check_min_confidence:
        return False
    review = rel.evidence.get("ai_review")
    return again or review is None or review.get("evidence_sha256") != evidence_hash(rel)


def evidence_hash(rel: Relationship) -> str:
    measured = {k: v for k, v in rel.evidence.items() if k != "ai_review"}
    return hashlib.sha256(json.dumps(measured, sort_keys=True, default=str).encode()).hexdigest()


def review_relationships(
    session: Session,
    source: DataSource,
    llm: LLMClient,
    *,
    now: datetime | None = None,
    again: bool = False,
    settings: DiscoverySettings = DEFAULT_DISCOVERY_SETTINGS,
) -> ReviewResult:
    now = now or utcnow()
    catalog = load_catalog(session, source)
    columns = {
        c.column_key: (t.ref, t.estimated_rows, c)
        for t in catalog.tables.values()
        for c in t.columns.values()
    }
    todo = [r for r in current_relationships(session, catalog) if needs_review(r, settings, again)]
    result = ReviewResult(llm.provider, llm.model, asked=len(todo))
    if not todo:
        return result

    by_id = {str(i + 1): rel for i, rel in enumerate(todo)}
    payload = {"links": [_describe(key, rel, columns) for key, rel in by_id.items()]}
    reply = llm.complete_json(SYSTEM_PROMPT, json.dumps(payload, indent=1, default=str))
    result.provider, result.model = reply.provider, reply.model
    for item in items_of(parse_json_reply(reply.text), "reviews"):
        rel = by_id.get(str(item.get("id")))
        verdict = item.get("verdict")
        confidence = confidence_of(item.get("confidence"))
        if rel is None or verdict not in VERDICTS or confidence is None:
            result.unusable += 1
            continue
        rel.evidence = {
            **rel.evidence,
            "ai_review": {
                "verdict": verdict,
                "confidence": confidence,
                "reason": str(item.get("reason") or "")[:500],
                "provider": reply.provider,
                "model": reply.model,
                "at": now.isoformat(),
                "evidence_sha256": evidence_hash(rel),
            },
        }
        result.reviewed += 1
        result.by_verdict[verdict] = result.by_verdict.get(verdict, 0) + 1
    session.flush()
    return result


def _describe(
    key: str,
    rel: Relationship,
    columns: dict[Any, tuple[str, int | None, ColumnInfo]],
) -> dict[str, Any]:
    pairs = [(columns.get(p.from_column_key), columns.get(p.to_column_key)) for p in rel.columns]
    sides = [(c, p) for c, p in pairs if c is not None and p is not None]
    evidence = {k: v for k, v in rel.evidence.items() if k != "ai_review"}
    return {
        "id": key,
        "child": _side([c for c, _ in sides]),
        "parent": _side([p for _, p in sides]),
        "tool_confidence": rel.confidence,
        "evidence": evidence,
    }


def _side(cols: list[tuple[str, int | None, ColumnInfo]]) -> dict[str, Any]:
    if not cols:
        return {}
    table, rows, _ = cols[0]
    return {
        "table": table,
        "rows": rows,
        "columns": [
            {
                "name": c.name,
                "type": c.native_type,
                "distinct": c.distinct_count,
                "nulls": c.null_count,
            }
            for _, _, c in cols
        ],
    }
