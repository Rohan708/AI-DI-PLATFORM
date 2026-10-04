"""The answer key: what was planted in the lab, written *before* detection runs."""

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ai_data_engineer.graph.models import FindingCategory
from ai_data_engineer.lab.schema import NORMAL_PATTERNS, TRUE_RELATIONSHIPS, ExpectedRelationship


@dataclass(frozen=True)
class ExpectedAnomaly:
    """One planted problem a detector should report.

    ``related_tables`` are tables the problem legitimately spills into (e.g. a half-loaded
    day also shrinks ``order_items``); findings there count as *related*, not false alarms.
    """

    scenario: str
    category: FindingCategory
    table: str  # "schema.table", exactly as named in the database
    column: str | None
    check_hint: str  # the kind of check expected to catch it, e.g. "null_rate_spike"
    detect_stage: str  # roadmap step whose detectors should catch it, e.g. "1.5"
    description: str
    effective_day: date  # first simulated day whose scan should show the problem
    details: dict[str, Any] = field(default_factory=dict)
    related_tables: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "category": self.category.value,
            "table": self.table,
            "column": self.column,
            "check_hint": self.check_hint,
            "detect_stage": self.detect_stage,
            "description": self.description,
            "effective_day": self.effective_day.isoformat(),
            "details": self.details,
            "related_tables": list(self.related_tables),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ExpectedAnomaly":
        return cls(
            scenario=data["scenario"],
            category=FindingCategory(data["category"]),
            table=data["table"],
            column=data["column"],
            check_hint=data["check_hint"],
            detect_stage=data["detect_stage"],
            description=data["description"],
            effective_day=date.fromisoformat(data["effective_day"]),
            details=dict(data["details"]),
            related_tables=tuple(data["related_tables"]),
        )


@dataclass(frozen=True)
class AnswerKey:
    seed: int
    size: str
    current_day: date
    anomalies: tuple[ExpectedAnomaly, ...]
    relationships: tuple[ExpectedRelationship, ...] = TRUE_RELATIONSHIPS
    normal_patterns: tuple[str, ...] = NORMAL_PATTERNS

    def to_json(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "size": self.size,
            "current_day": self.current_day.isoformat(),
            "anomalies": [a.to_json() for a in self.anomalies],
            "relationships": [
                {
                    "from_table": r.from_table,
                    "from_columns": list(r.from_columns),
                    "to_table": r.to_table,
                    "to_columns": list(r.to_columns),
                    "declared": r.declared,
                    "note": r.note,
                }
                for r in self.relationships
            ],
            "normal_patterns": list(self.normal_patterns),
        }

    def to_markdown(self) -> str:
        out = [
            f"# Lab answer key — seed {self.seed}, size `{self.size}`",
            "",
            f"Simulated through **{self.current_day.isoformat()}**. Written by the lab "
            "*before* detection runs; the scorer compares findings with it.",
            "",
            f"## Planted anomalies ({len(self.anomalies)})",
            "",
            "| # | Scenario | Category | Where | Expected check | Stage | Effective | What |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for i, a in enumerate(self.anomalies, start=1):
            where = f"`{a.table}.{a.column}`" if a.column else f"`{a.table}`"
            out.append(
                f"| {i} | {a.scenario} | {a.category.value} | {where} | {a.check_hint} | "
                f"{a.detect_stage} | {a.effective_day.isoformat()} | {a.description} |"
            )
        hidden = [r for r in self.relationships if not r.declared]
        out += [
            "",
            f"## True relationships ({len(self.relationships)}; {len(hidden)} hidden)",
            "",
            "| From | To | Declared FK? | Note |",
            "|---|---|---|---|",
        ]
        for r in self.relationships:
            out.append(
                f"| `{r.from_table}({', '.join(r.from_columns)})` | "
                f"`{r.to_table}({', '.join(r.to_columns)})` | "
                f"{'yes' if r.declared else '**no**'} | {r.note} |"
            )
        out += ["", "## Normal patterns (must NOT be flagged)", ""]
        out += [f"- {pattern}" for pattern in self.normal_patterns]
        return "\n".join(out) + "\n"


def write_answer_key(key: AnswerKey, directory: Path, name: str) -> tuple[Path, Path]:
    """Write ``<name>.json`` (for the scorer) and ``<name>.md`` (for humans)."""
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{name}.json"
    md_path = directory / f"{name}.md"
    json_path.write_text(json.dumps(key.to_json(), indent=2), encoding="utf-8")
    md_path.write_text(key.to_markdown(), encoding="utf-8")
    return json_path, md_path
