"""Scoring AI rule proposals against the lab's hidden rules (``HIDDEN_RULES``).

A proposal matches a hidden rule when the kind, table and column agree and the compared
or summed column is the same (a same-row comparison may be written either way round).
The operator isn't scored. Proposals that match nothing aren't necessarily wrong (the
answer key can't list every true rule): a person's approve/reject decides, and the
report counts those decisions.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.graph.models import Origin, Rule, RuleStatus
from ai_data_engineer.lab.schema import ExpectedRule
from ai_data_engineer.rules.spec import (
    CompareColumns,
    CompareConstant,
    RuleSpec,
    SumMatches,
    Via,
    parse_spec,
)


@dataclass(frozen=True)
class FoundRule:
    kind: str
    table: str
    column: str
    other: str | None
    status: RuleStatus
    text: str


@dataclass
class RuleScore:
    found: list[ExpectedRule] = field(default_factory=list)
    missed: list[ExpectedRule] = field(default_factory=list)
    unmatched: list[FoundRule] = field(default_factory=list)  # not in the answer key
    proposals: int = 0
    approved: int = 0
    rejected: int = 0

    def to_markdown(self) -> str:
        expected = len(self.found) + len(self.missed)
        out = [
            "## AI rule proposals",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Hidden rules proposed (recall) | {len(self.found)} / {expected} |",
            f"| AI proposals | {self.proposals} |",
            f"| Approved / rejected by a person | {self.approved} / {self.rejected} |",
            f"| Proposals not in the answer key | {len(self.unmatched)} |",
        ]
        if self.missed:
            out += ["", "Hidden rules not proposed:", ""]
            out += [f"- `{_text(r)}`: {r.note}" for r in self.missed]
        if self.unmatched:
            out += ["", "Other proposals (judged by review, not by the answer key):", ""]
            out += [f"- `{r.text}` ({r.status.value})" for r in self.unmatched]
        return "\n".join(out) + "\n"


def found_rule(spec: RuleSpec, status: RuleStatus, text: str) -> FoundRule:
    match spec:
        case CompareColumns(via=Via() as via):
            other: str | None = f"{via.parent_table}.{spec.other_column}"
        case CompareColumns():
            other = spec.other_column
        case SumMatches():
            other = f"{spec.child_table}.{spec.child_column}"
        case CompareConstant():
            other = None
    return FoundRule(spec.kind, spec.table, spec.column, other, status, text)


def rule_matches(expected: ExpectedRule, found: FoundRule) -> bool:
    if (expected.kind, expected.table) != (found.kind, found.table):
        return False
    same_way = (expected.column, expected.other) == (found.column, found.other)
    flipped = (
        expected.kind == "compare_columns"
        and expected.other is not None
        and "." not in expected.other  # same-row comparison
        and (expected.column, expected.other) == (found.other, found.column)
    )
    return same_way or flipped


def score_rules(expected: tuple[ExpectedRule, ...], found: list[FoundRule]) -> RuleScore:
    report = RuleScore(proposals=len(found))
    report.approved = sum(1 for f in found if f.status in (RuleStatus.ACTIVE, RuleStatus.DISABLED))
    report.rejected = sum(1 for f in found if f.status is RuleStatus.REJECTED)
    matched: set[int] = set()
    for rule in expected:
        hits = [i for i, f in enumerate(found) if rule_matches(rule, f)]
        (report.found if hits else report.missed).append(rule)
        matched.update(hits)
    report.unmatched = [f for i, f in enumerate(found) if i not in matched]
    return report


def load_ai_rules(session: Session, data_source_id: uuid.UUID) -> list[FoundRule]:
    rules = session.scalars(
        select(Rule).where(Rule.data_source_id == data_source_id, Rule.origin == Origin.AI)
    )
    return [found_rule(parse_spec(r.definition["spec"]), r.status, r.name) for r in rules]


def _text(r: ExpectedRule) -> str:
    if r.kind == "compare_constant":
        return f"{r.table}.{r.column} (vs a constant)"
    if r.kind == "sum_matches":
        return f"{r.table}.{r.column} = sum({r.other})"
    return f"{r.table}.{r.column} vs {r.other}"
