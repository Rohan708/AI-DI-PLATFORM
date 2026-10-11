"""Importing what a dbt project already says about its tables (``schema.yml``).

A team that documents its models in dbt shouldn't have to type it again:

| In ``schema.yml`` | Becomes |
|---|---|
| model / source table ``description`` | the table's description |
| column ``description`` | the column's description |
| test ``not_null`` | a ``not_null`` rule (active) |
| test ``unique`` | a ``unique`` rule (active) |
| test ``accepted_values: {values: [...]}`` | an ``allowed_values`` rule (active) |
| test ``relationships: {to: ref('x'), field: y}`` | the discovered relationship confirmed |

Both ``tests:`` and dbt 1.8+'s ``data_tests:`` are read, and arguments given directly or
under ``arguments:``. Rules are created by ``dbt:<file name>``; nothing runs dbt itself.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from ai_data_engineer.context.store import set_note
from ai_data_engineer.discovery.catalog import Catalog, TableInfo, load_catalog
from ai_data_engineer.discovery.store import current_relationships, review
from ai_data_engineer.graph.models import (
    DataSource,
    Origin,
    RelationshipStatus,
    RuleStatus,
)
from ai_data_engineer.rules.spec import AllowedValues, NotNull, RuleSpec, Unique, signature
from ai_data_engineer.rules.store import rule_with_signature, save_rule

_REF = re.compile(r"""(?:ref|source)\(\s*(?:['"][^'"]+['"]\s*,\s*)?['"]([^'"]+)['"]\s*\)""")


@dataclass(frozen=True)
class DbtColumn:
    name: str
    description: str
    tests: list[tuple[str, dict[str, Any]]]  # (test name, arguments)


@dataclass(frozen=True)
class DbtModel:
    name: str
    description: str
    columns: list[DbtColumn]


@dataclass
class DbtImportResult:
    file: str
    tables_matched: list[str] = field(default_factory=list)
    tables_unmatched: list[str] = field(default_factory=list)
    descriptions: int = 0
    rules_added: list[str] = field(default_factory=list)
    rules_existing: int = 0
    links_confirmed: list[str] = field(default_factory=list)
    links_not_found: list[str] = field(default_factory=list)
    skipped_tests: list[str] = field(default_factory=list)  # test kinds we don't map

    def summary(self) -> str:
        return (
            f"{self.file}: {len(self.tables_matched)} tables matched "
            f"({len(self.tables_unmatched)} not found), {self.descriptions} descriptions, "
            f"{len(self.rules_added)} rules added ({self.rules_existing} already there), "
            f"{len(self.links_confirmed)} relationships confirmed "
            f"({len(self.links_not_found)} not discovered)"
        )


def parse_dbt(text: str) -> list[DbtModel]:
    """Models and source tables from a dbt ``schema.yml`` (pure)."""
    data = yaml.safe_load(text) or {}
    entries: list[dict[str, Any]] = list(data.get("models") or [])
    for src in data.get("sources") or []:
        entries += list(src.get("tables") or [])
    out = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        columns = [
            DbtColumn(
                name=str(c["name"]),
                description=str(c.get("description") or ""),
                tests=[_test(t) for t in (c.get("data_tests") or c.get("tests") or [])],
            )
            for c in entry.get("columns") or []
            if isinstance(c, dict) and c.get("name")
        ]
        out.append(DbtModel(str(entry["name"]), str(entry.get("description") or ""), columns))
    return out


def _test(raw: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(raw, str):
        return raw, {}
    if isinstance(raw, dict) and raw:
        name, args = next(iter(raw.items()))
        args = args if isinstance(args, dict) else {}
        merged = {**args, **(args.get("arguments") or {})}
        return str(name), merged
    return "unknown", {}


def import_dbt(
    session: Session,
    source: DataSource,
    path: Path,
    *,
    by: str,
    now: datetime,
    schema: str | None = None,
) -> DbtImportResult:
    models = parse_dbt(path.read_text(encoding="utf-8"))
    catalog = load_catalog(session, source)
    author = f"dbt:{path.name}"
    result = DbtImportResult(path.name)
    for model in models:
        table = catalog.find(schema or "", model.name)
        if table is None:
            result.tables_unmatched.append(model.name)
            continue
        result.tables_matched.append(table.ref)
        if model.description:
            set_note(session, source, "description", {"text": model.description},
                     by=author, now=now, asset_key=table.asset_key)  # fmt: skip
            result.descriptions += 1
        for column in model.columns:
            info = next((c for c in table.columns.values()
                         if c.name.lower() == column.name.lower()), None)  # fmt: skip
            if info is None:
                continue
            if column.description:
                set_note(session, source, "description", {"text": column.description},
                         by=author, now=now, asset_key=table.asset_key,
                         column_key=info.column_key)  # fmt: skip
                result.descriptions += 1
            for name, args in column.tests:
                _apply_test(session, source, catalog, table, info.name, name, args,
                            author, now, result)  # fmt: skip
    return result


def _apply_test(
    session: Session,
    source: DataSource,
    catalog: Catalog,
    table: TableInfo,
    column: str,
    name: str,
    args: dict[str, Any],
    author: str,
    now: datetime,
    result: DbtImportResult,
) -> None:
    spec: RuleSpec
    if name == "not_null":
        spec = NotNull(table=table.ref, column=column)
    elif name == "unique":
        spec = Unique(table=table.ref, column=column)
    elif name == "accepted_values" and args.get("values"):
        spec = AllowedValues(table=table.ref, column=column,
                             values=[str(v) for v in args["values"]])  # fmt: skip
    elif name == "relationships":
        _confirm_link(session, catalog, table, column, args, author, now, result)
        return
    else:
        result.skipped_tests.append(f"{table.ref}.{column}: {name}")
        return
    if rule_with_signature(session, source, signature(spec)) is not None:
        result.rules_existing += 1
        return
    save_rule(
        session, source=source, catalog=catalog, spec=spec, origin=Origin.USER,
        status=RuleStatus.ACTIVE, created_by=author, now=now,
    )  # fmt: skip
    result.rules_added.append(f"{table.ref}.{column}: {name}")


def _confirm_link(
    session: Session,
    catalog: Catalog,
    table: TableInfo,
    column: str,
    args: dict[str, Any],
    author: str,
    now: datetime,
    result: DbtImportResult,
) -> None:
    match = _REF.search(str(args.get("to", "")))
    field_name = str(args.get("field", ""))
    text = f"{table.ref}.{column} -> {match.group(1) if match else args.get('to')}.{field_name}"
    parent = catalog.find("", match.group(1)) if match else None
    if parent is None:
        result.links_not_found.append(text)
        return
    child_col = table.columns.get(column)
    parent_col = next(
        (c for c in parent.columns.values() if c.name.lower() == field_name.lower()), None
    )
    for rel in current_relationships(session, catalog):
        pairs = [(p.from_column_key, p.to_column_key) for p in rel.columns]
        if child_col and parent_col and pairs == [(child_col.column_key, parent_col.column_key)]:
            if rel.status is not RelationshipStatus.CONFIRMED:
                review(session, rel, RelationshipStatus.CONFIRMED, author, now)
            result.links_confirmed.append(text)
            return
    result.links_not_found.append(text)
