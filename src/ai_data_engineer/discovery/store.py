"""Saving discovered relationships, respecting human decisions.

- Declared FKs are stored as ``declared`` / ``confirmed`` (no confidence needed).
- Inferred links are stored as ``inferred`` / ``proposed`` with confidence + evidence.
- A relationship a person confirmed or rejected keeps its status forever; discovery
  only refreshes its evidence.
- Relationships discovery no longer supports are closed (``valid_to``), except ones a
  person reviewed. History is kept: closed rows stay queryable.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.discovery.catalog import Catalog
from ai_data_engineer.graph.models import (
    Asset,
    Relationship,
    RelationshipColumn,
    RelationshipKind,
    RelationshipStatus,
    relationship_signature,
)


@dataclass(frozen=True)
class Proposal:
    child_ref: str
    child_columns: tuple[str, ...]
    parent_ref: str
    parent_columns: tuple[str, ...]
    kind: RelationshipKind
    confidence: float | None
    signals: tuple[str, ...]
    evidence: dict[str, Any]


@dataclass
class SyncResult:
    created: list[Relationship] = field(default_factory=list)
    updated: list[Relationship] = field(default_factory=list)
    retired: list[Relationship] = field(default_factory=list)


def is_reviewed(rel: Relationship) -> bool:
    return rel.reviewed_at is not None


def sync_relationships(
    session: Session, catalog: Catalog, proposals: list[Proposal], now: datetime
) -> SyncResult:
    result = SyncResult()
    tenant_id = catalog.source.tenant_id
    supported: set[str] = set()

    for proposal in proposals:
        child = catalog.tables[proposal.child_ref]
        parent = catalog.tables[proposal.parent_ref]
        pairs = [
            (child.columns[c].column_key, parent.columns[p].column_key)
            for c, p in zip(proposal.child_columns, proposal.parent_columns, strict=True)
        ]
        signature = relationship_signature(pairs)
        supported.add(signature)
        current = _current(session, tenant_id, signature)
        if current is None:
            declared = proposal.kind is RelationshipKind.DECLARED
            rel = Relationship(
                tenant_id=tenant_id,
                from_asset_key=child.asset_key,
                to_asset_key=parent.asset_key,
                kind=proposal.kind,
                status=RelationshipStatus.CONFIRMED if declared else RelationshipStatus.PROPOSED,
                confidence=proposal.confidence,
                signals=list(proposal.signals),
                evidence=proposal.evidence,
                signature=signature,
                valid_from=now,
                columns=[
                    RelationshipColumn(
                        tenant_id=tenant_id, ordinal=i, from_column_key=f, to_column_key=t
                    )
                    for i, (f, t) in enumerate(pairs)
                ],
            )
            session.add(rel)
            result.created.append(rel)
            continue
        current.evidence = proposal.evidence
        current.signals = list(proposal.signals)
        if current.kind is RelationshipKind.INFERRED and not is_reviewed(current):
            current.confidence = proposal.confidence
        result.updated.append(current)

    for rel in current_relationships(session, catalog):
        if rel.signature in supported or is_reviewed(rel):
            continue
        rel.valid_to = max(now, rel.valid_from)
        result.retired.append(rel)
    session.flush()
    return result


def current_relationships(session: Session, catalog: Catalog) -> list[Relationship]:
    """Current (open) relationships whose child table belongs to this source."""
    return list(
        session.scalars(
            select(Relationship)
            .join(Asset, Asset.asset_key == Relationship.from_asset_key)
            .where(
                Relationship.tenant_id == catalog.source.tenant_id,
                Asset.data_source_id == catalog.source.id,
                Relationship.valid_to.is_(None),
            )
        )
    )


def review(
    session: Session, rel: Relationship, status: RelationshipStatus, reviewer: str, now: datetime
) -> None:
    rel.status = status
    rel.reviewed_by = reviewer
    rel.reviewed_at = now
    session.flush()


def find_relationship(session: Session, id_prefix: str) -> Relationship:
    """Find a current relationship by (a prefix of) its id, as printed by the CLI."""
    matches = [
        rel
        for rel in session.scalars(select(Relationship).where(Relationship.valid_to.is_(None)))
        if str(rel.id).startswith(id_prefix)
    ]
    if len(matches) != 1:
        raise LookupError(f"{len(matches)} relationships match id prefix {id_prefix!r}")
    return matches[0]


def _current(session: Session, tenant_id: uuid.UUID, signature: str) -> Relationship | None:
    return session.scalar(
        select(Relationship).where(
            Relationship.tenant_id == tenant_id,
            Relationship.signature == signature,
            Relationship.valid_to.is_(None),
        )
    )
