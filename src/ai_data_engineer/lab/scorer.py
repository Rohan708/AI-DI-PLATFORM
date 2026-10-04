"""The benchmark: compare what our detectors found with the lab's answer key.

Matching rule: a finding catches a planted anomaly when it has the same **category** and
**table**, and the columns agree (either side may be table-level, i.e. no column).
Findings on an anomaly's ``related_tables`` count as *related* (expected knock-on
effects), not as false alarms. Everything else is a false alarm.

``score`` is pure; ``load_findings`` / ``load_relationships`` read the metadata store.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from ai_data_engineer.graph.models import (
    Asset,
    AssetColumn,
    Finding,
    FindingCategory,
    Relationship,
    RelationshipColumn,
    RelationshipStatus,
)
from ai_data_engineer.lab.answer_key import AnswerKey, ExpectedAnomaly
from ai_data_engineer.lab.schema import ExpectedRelationship


@dataclass(frozen=True)
class FoundFinding:
    category: FindingCategory
    table: str | None  # "schema.table"
    column: str | None
    check_name: str
    title: str


@dataclass(frozen=True)
class FoundRelationship:
    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]


@dataclass
class CategoryScore:
    expected: int = 0
    caught: int = 0
    false_alarms: int = 0


@dataclass
class ScoreReport:
    by_category: dict[FindingCategory, CategoryScore]
    caught: list[tuple[ExpectedAnomaly, list[FoundFinding]]] = field(default_factory=list)
    missed: list[ExpectedAnomaly] = field(default_factory=list)
    related: list[FoundFinding] = field(default_factory=list)
    false_alarms: list[FoundFinding] = field(default_factory=list)
    relationships_found: list[ExpectedRelationship] = field(default_factory=list)
    relationships_missed: list[ExpectedRelationship] = field(default_factory=list)
    relationships_false: list[FoundRelationship] = field(default_factory=list)

    @property
    def recall(self) -> float | None:
        expected = len(self.caught) + len(self.missed)
        return len(self.caught) / expected if expected else None

    @property
    def precision(self) -> float | None:
        true_findings = sum(len(findings) for _, findings in self.caught)
        produced = true_findings + len(self.false_alarms)
        return true_findings / produced if produced else None

    @property
    def relationship_recall(self) -> float | None:
        expected = len(self.relationships_found) + len(self.relationships_missed)
        return len(self.relationships_found) / expected if expected else None

    @property
    def relationship_precision(self) -> float | None:
        produced = len(self.relationships_found) + len(self.relationships_false)
        return len(self.relationships_found) / produced if produced else None

    def to_json(self) -> dict[str, Any]:
        return {
            "recall": self.recall,
            "precision": self.precision,
            "relationship_recall": self.relationship_recall,
            "relationship_precision": self.relationship_precision,
            "by_category": {
                category.value: vars(counts) for category, counts in self.by_category.items()
            },
            "missed": [a.scenario + ":" + _where(a.table, a.column) for a in self.missed],
            "false_alarms": [
                f"{f.check_name}:{_where(f.table, f.column)}" for f in self.false_alarms
            ],
            "relationships_missed": [_rel_text(r) for r in self.relationships_missed],
            "relationships_false": [_rel_text(r) for r in self.relationships_false],
        }

    def to_markdown(self) -> str:
        out = [
            "# Lab benchmark",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Anomalies caught (recall) | {len(self.caught)} / "
            f"{len(self.caught) + len(self.missed)} ({_pct(self.recall)}) |",
            f"| Findings that were real (precision) | {_pct(self.precision)} |",
            f"| False alarms | {len(self.false_alarms)} |",
            f"| Related knock-on findings (not penalised) | {len(self.related)} |",
            f"| Relationships discovered (recall) | {len(self.relationships_found)} / "
            f"{len(self.relationships_found) + len(self.relationships_missed)} "
            f"({_pct(self.relationship_recall)}) |",
            f"| Wrong relationships proposed | {len(self.relationships_false)} |",
            "",
            "## By category",
            "",
            "| Category | Planted | Caught | False alarms |",
            "|---|---|---|---|",
        ]
        for category, s in self.by_category.items():
            if s.expected or s.false_alarms:
                out.append(f"| {category.value} | {s.expected} | {s.caught} | {s.false_alarms} |")
        if self.missed:
            out += [
                "",
                "## Missed",
                "",
                "| Scenario | Where | Expected check | Stage |",
                "|---|---|---|---|",
            ]
            out += [
                f"| {a.scenario} | `{_where(a.table, a.column)}` | "
                f"{a.check_hint} | {a.detect_stage} |"
                for a in self.missed
            ]
        if self.false_alarms:
            out += ["", "## False alarms", "", "| Check | Where | Title |", "|---|---|---|"]
            out += [
                f"| {f.check_name} | `{_where(f.table, f.column)}` | {f.title} |"
                for f in self.false_alarms
            ]
        if self.relationships_missed:
            out += ["", "## Relationships not discovered", ""]
            out += [f"- `{_rel_text(r)}`" for r in self.relationships_missed]
        if self.relationships_false:
            out += ["", "## Wrong relationships", ""]
            out += [f"- `{_rel_text(r)}`" for r in self.relationships_false]
        return "\n".join(out) + "\n"


def matches(expected: ExpectedAnomaly, finding: FoundFinding) -> bool:
    if finding.category != expected.category or finding.table != expected.table:
        return False
    return expected.column is None or finding.column is None or finding.column == expected.column


def score(
    key: AnswerKey,
    findings: list[FoundFinding],
    relationships: list[FoundRelationship],
) -> ScoreReport:
    report = ScoreReport(by_category={category: CategoryScore() for category in FindingCategory})
    matched: set[int] = set()

    for anomaly in key.anomalies:
        report.by_category[anomaly.category].expected += 1
        hits = [f for f in findings if matches(anomaly, f)]
        if hits:
            report.by_category[anomaly.category].caught += 1
            report.caught.append((anomaly, hits))
            matched.update(id(f) for f in hits)
        else:
            report.missed.append(anomaly)

    related_tables = {t for anomaly in key.anomalies for t in anomaly.related_tables}
    for finding in findings:
        if id(finding) in matched:
            continue
        if finding.table in related_tables:
            report.related.append(finding)
        else:
            report.false_alarms.append(finding)
            report.by_category[finding.category].false_alarms += 1

    expected_by_signature = {_signature(r): r for r in key.relationships}
    found_signatures = set()
    for found in relationships:
        signature = _signature(found)
        found_signatures.add(signature)
        if signature not in expected_by_signature:
            report.relationships_false.append(found)
    for signature, expected in expected_by_signature.items():
        if signature in found_signatures:
            report.relationships_found.append(expected)
        else:
            report.relationships_missed.append(expected)
    return report


# --- loading from the metadata store ----------------------------------------------------


def load_findings(session: Session, data_source_id: uuid.UUID) -> list[FoundFinding]:
    rows = session.execute(
        select(Finding, Asset.schema_name, Asset.name, AssetColumn.name)
        .outerjoin(Asset, Asset.asset_key == Finding.asset_key)
        .outerjoin(AssetColumn, AssetColumn.column_key == Finding.column_key)
        .where(Finding.data_source_id == data_source_id)
    ).all()
    return [
        FoundFinding(
            category=finding.category,
            table=f"{schema}.{table}" if table else None,
            column=column,
            check_name=finding.check_name,
            title=finding.title,
        )
        for finding, schema, table, column in rows
    ]


def load_relationships(session: Session, data_source_id: uuid.UUID) -> list[FoundRelationship]:
    """Current, non-rejected relationships whose child table belongs to the source."""
    from_asset, to_asset = aliased(Asset), aliased(Asset)
    from_col, to_col = aliased(AssetColumn), aliased(AssetColumn)
    rows = session.execute(
        select(
            Relationship.id,
            from_asset.schema_name,
            from_asset.name,
            to_asset.schema_name,
            to_asset.name,
            from_col.name,
            to_col.name,
        )
        .join(from_asset, from_asset.asset_key == Relationship.from_asset_key)
        .join(to_asset, to_asset.asset_key == Relationship.to_asset_key)
        .join(RelationshipColumn, RelationshipColumn.relationship_id == Relationship.id)
        .join(from_col, from_col.column_key == RelationshipColumn.from_column_key)
        .join(to_col, to_col.column_key == RelationshipColumn.to_column_key)
        .where(
            from_asset.data_source_id == data_source_id,
            Relationship.valid_to.is_(None),
            Relationship.status != RelationshipStatus.REJECTED,
        )
        .order_by(Relationship.id, RelationshipColumn.ordinal)
    ).all()

    grouped: dict[uuid.UUID, dict[str, Any]] = {}
    for rel_id, f_schema, f_table, t_schema, t_table, f_col, t_col in rows:
        entry = grouped.setdefault(
            rel_id,
            {"from": f"{f_schema}.{f_table}", "to": f"{t_schema}.{t_table}", "fc": [], "tc": []},
        )
        entry["fc"].append(f_col)
        entry["tc"].append(t_col)
    return [
        FoundRelationship(e["from"], tuple(e["fc"]), e["to"], tuple(e["tc"]))
        for e in grouped.values()
    ]


# --- helpers ----------------------------------------------------------------------------


def _signature(
    r: ExpectedRelationship | FoundRelationship,
) -> tuple[str, str, frozenset[tuple[str, str]]]:
    return (r.from_table, r.to_table, frozenset(zip(r.from_columns, r.to_columns, strict=True)))


def _where(table: str | None, column: str | None) -> str:
    if table is None:
        return "(source-level)"
    return f"{table}.{column}" if column else table


def _rel_text(r: ExpectedRelationship | FoundRelationship) -> str:
    return f"{r.from_table}({', '.join(r.from_columns)}) -> {r.to_table}({', '.join(r.to_columns)})"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"
