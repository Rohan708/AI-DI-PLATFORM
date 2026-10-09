"""Storing business rules and the human decisions about them.

A rule's ``definition`` holds its spec (see ``spec.py``) and signature. AI proposals
start ``proposed`` and run only after a person makes them ``active``; a rule someone
rejected is never proposed again (same signature).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.discovery.catalog import Catalog
from ai_data_engineer.discovery.store import current_relationships
from ai_data_engineer.graph.models import (
    DataSource,
    Origin,
    RelationshipStatus,
    Rule,
    RuleStatus,
)
from ai_data_engineer.rules.spec import (
    Link,
    RuleSpec,
    describe,
    parse_spec,
    signature,
    spec_json,
    subject,
    validate,
)

ALLOWED_RULE_TRANSITIONS: dict[RuleStatus, set[RuleStatus]] = {
    RuleStatus.PROPOSED: {RuleStatus.ACTIVE, RuleStatus.REJECTED},
    RuleStatus.ACTIVE: {RuleStatus.DISABLED},
    RuleStatus.DISABLED: {RuleStatus.ACTIVE},
    RuleStatus.REJECTED: {RuleStatus.PROPOSED},  # reconsider
}


class InvalidRuleError(ValueError):
    pass


def known_relationships(
    session: Session, catalog: Catalog
) -> list[tuple[Link, str, float | None]]:
    """Every current relationship nobody rejected, as (link, status, confidence)."""
    names = {
        column.column_key: (table.ref, column.name)
        for table in catalog.tables.values()
        for column in table.columns.values()
    }
    found: list[tuple[Link, str, float | None]] = []
    for rel in current_relationships(session, catalog):
        if rel.status is RelationshipStatus.REJECTED:
            continue
        children = [names.get(p.from_column_key) for p in rel.columns]
        parents = [names.get(p.to_column_key) for p in rel.columns]
        child_refs = [c for c in children if c is not None]
        parent_refs = [p for p in parents if p is not None]
        if not child_refs or len(child_refs) != len(children) or len(parent_refs) != len(parents):
            continue  # a column was dropped since
        link: Link = (
            child_refs[0][0],
            tuple(c[1] for c in child_refs),
            parent_refs[0][0],
            tuple(p[1] for p in parent_refs),
        )
        found.append((link, rel.status.value, rel.confidence))
    return found


def known_links(session: Session, catalog: Catalog) -> list[Link]:
    """Relationships a rule may join through."""
    return [link for link, _, _ in known_relationships(session, catalog)]


def rule_spec(rule: Rule) -> RuleSpec:
    return parse_spec(rule.definition["spec"])


def rules_of(session: Session, source: DataSource, status: RuleStatus | None = None) -> list[Rule]:
    query = select(Rule).where(Rule.data_source_id == source.id).order_by(Rule.created_at)
    if status is not None:
        query = query.where(Rule.status == status)
    return list(session.scalars(query))


def rule_with_signature(session: Session, source: DataSource, sig: str) -> Rule | None:
    matches = (r for r in rules_of(session, source) if r.definition.get("signature") == sig)
    return next(matches, None)


def save_rule(
    session: Session,
    *,
    source: DataSource,
    catalog: Catalog,
    spec: RuleSpec,
    origin: Origin,
    status: RuleStatus,
    created_by: str,
    now: datetime,
    confidence: float | None = None,
    evidence: dict[str, Any] | None = None,
) -> Rule:
    table_ref, column_name = subject(spec)
    table = catalog.tables[table_ref]
    rule = Rule(
        tenant_id=source.tenant_id,
        data_source_id=source.id,
        name=describe(spec)[:255],
        rule_type=spec.kind,
        asset_key=table.asset_key,
        column_key=table.columns[column_name].column_key,
        definition={"spec": spec_json(spec), "signature": signature(spec)},
        origin=origin,
        status=status,
        confidence=confidence,
        evidence=evidence or {},
        created_at=now,
        created_by=created_by,
    )
    session.add(rule)
    session.flush()
    return rule


def add_user_rule(
    session: Session, source: DataSource, catalog: Catalog, spec: RuleSpec, by: str, now: datetime
) -> Rule:
    """A rule written by a person: checked like a proposal, active at once."""
    problems = validate(spec, catalog, known_links(session, catalog))
    if problems:
        raise InvalidRuleError("; ".join(problems))
    if rule_with_signature(session, source, signature(spec)) is not None:
        raise InvalidRuleError(f"this rule already exists: {describe(spec)}")
    return save_rule(
        session, source=source, catalog=catalog, spec=spec, origin=Origin.USER,
        status=RuleStatus.ACTIVE, created_by=by, now=now,
    )  # fmt: skip


def review_rule(
    session: Session,
    rule: Rule,
    to_status: RuleStatus,
    *,
    actor: str,
    now: datetime,
    note: str | None = None,
) -> None:
    if to_status not in ALLOWED_RULE_TRANSITIONS[rule.status]:
        raise InvalidRuleError(
            f"a rule that is {rule.status.value} can't become {to_status.value}"
        )
    history = list(rule.evidence.get("review_history", []))
    history.append(
        {
            "at": now.isoformat(),
            "by": actor,
            "from": rule.status.value,
            "to": to_status.value,
            "note": note,
        }
    )
    rule.evidence = {**rule.evidence, "review_history": history}
    rule.status = to_status
    rule.reviewed_by, rule.reviewed_at = actor, now
    session.flush()


def find_rule(session: Session, id_prefix: str) -> Rule:
    matches = [r for r in session.scalars(select(Rule)) if str(r.id).startswith(id_prefix)]
    if len(matches) != 1:
        raise LookupError(f"{len(matches)} rules match id prefix {id_prefix!r}")
    return matches[0]


def rule_ref(rule_id: uuid.UUID) -> str:
    return str(rule_id)[:8]
