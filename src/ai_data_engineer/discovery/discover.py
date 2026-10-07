"""Relationship discovery for one source.

1. load what we know (latest scan): tables, columns, measurements, keys
2. declared FKs -> relationships (certain)
3. read the query log -> joins the application runs
4. generate candidates (name / type / measurement gates), rank them, and check the top
   ``max_inclusion_checks`` in the database (row-level value overlap)
5. score -> keep >= propose_threshold -> pick one explanation per child column(s)
6. save (respecting human reviews), then run relationship checks -> findings

The caller owns the transaction.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.discovery.candidates import Candidate, generate_candidates
from ai_data_engineer.discovery.catalog import Catalog, load_catalog
from ai_data_engineer.discovery.checks import CheckResult, run_relationship_checks
from ai_data_engineer.discovery.querylog import ColumnRef, join_counts
from ai_data_engineer.discovery.scoring import choose_best, score
from ai_data_engineer.discovery.settings import DEFAULT_DISCOVERY_SETTINGS, DiscoverySettings
from ai_data_engineer.discovery.store import (
    Proposal,
    SyncResult,
    current_relationships,
    sync_relationships,
)
from ai_data_engineer.graph.models import (
    DataSource,
    IngestionRun,
    RelationshipKind,
    utcnow,
)
from ai_data_engineer.ingestion.base import KeyRef
from ai_data_engineer.ingestion.scan import AdapterFactory, open_adapter

DECLARED_SIGNAL = "declared_fk"


@dataclass
class DiscoveryResult:
    declared: int = 0
    candidates: int = 0
    inclusion_checks: int = 0
    skipped_over_limit: int = 0
    query_log_statements: int = 0
    proposed: list[Candidate] = field(default_factory=list)
    sync: SyncResult = field(default_factory=SyncResult)
    checks: CheckResult = field(default_factory=CheckResult)

    def summary(self) -> str:
        return (
            f"{self.declared} declared, {len(self.proposed)} inferred "
            f"({self.inclusion_checks} overlap checks on {self.candidates} candidates; "
            f"{self.query_log_statements} logged statements read); "
            f"relationships: {len(self.sync.created)} new, {len(self.sync.retired)} retired; "
            f"findings: {self.checks.opened} new, {self.checks.refreshed} still open, "
            f"{self.checks.resolved} resolved"
        )


def discover(
    session: Session,
    source: DataSource,
    *,
    adapter_factory: AdapterFactory = open_adapter,
    now: datetime | None = None,
    settings: DiscoverySettings = DEFAULT_DISCOVERY_SETTINGS,
) -> DiscoveryResult:
    now = now or _latest_scan_time(session, source) or utcnow()
    catalog = load_catalog(session, source)
    result = DiscoveryResult()

    declared = _declared(catalog)
    result.declared = len(declared)
    declared_children = {(p.child_ref, p.child_columns) for p in declared}

    with adapter_factory(source) as adapter:
        stats = adapter.read_query_log(settings.query_log_limit)
        result.query_log_statements = len(stats)
        joins: Counter[frozenset[ColumnRef]] = Counter()
        for pair, calls in join_counts(stats).items():
            joins[_canonical(pair, catalog)] += calls

        candidates = generate_candidates(catalog, declared_children, settings)
        for c in candidates:
            c.query_calls = _calls(c, joins)
        candidates = [c for c in candidates if not (c.needs_query_log and not c.query_calls)]
        candidates.sort(key=lambda c: c.prior, reverse=True)
        result.candidates = len(candidates)
        to_check = candidates[: settings.max_inclusion_checks]
        result.skipped_over_limit = len(candidates) - len(to_check)

        accepted = []
        for c in to_check:
            overlap = adapter.value_inclusion(
                KeyRef(c.child.schema_name, c.child.name, c.child_columns, c.child.estimated_rows),
                KeyRef(c.parent.schema_name, c.parent.name, c.parent_columns),
                settings.max_values_checked,
            )
            result.inclusion_checks += 1
            c.inclusion, c.values_checked = overlap.inclusion, overlap.checked_rows
            if score(c, settings) >= settings.propose_threshold:
                accepted.append(c)
        result.proposed = choose_best(accepted)

        proposals = declared + [_proposal(c) for c in result.proposed]
        result.sync = sync_relationships(session, catalog, proposals, now)
        result.checks = run_relationship_checks(
            session, catalog, current_relationships(session, catalog), adapter, now, settings
        )
    return result


def _declared(catalog: Catalog) -> list[Proposal]:
    proposals = []
    for table in catalog.tables.values():
        for fk in table.foreign_keys:
            schema, _, name = fk["ref_table"].partition(".")
            parent = catalog.find(schema, name)
            if parent is None:
                continue  # references a table outside the scanned schemas
            proposals.append(
                Proposal(
                    child_ref=table.ref,
                    child_columns=tuple(fk["columns"]),
                    parent_ref=parent.ref,
                    parent_columns=tuple(fk["ref_columns"]),
                    kind=RelationshipKind.DECLARED,
                    confidence=None,
                    signals=(DECLARED_SIGNAL,),
                    evidence={"constraint": fk["name"]},
                )
            )
    return proposals


def _proposal(c: Candidate) -> Proposal:
    return Proposal(
        child_ref=c.child.ref,
        child_columns=c.child_columns,
        parent_ref=c.parent.ref,
        parent_columns=c.parent_columns,
        kind=RelationshipKind.INFERRED,
        confidence=c.confidence,
        signals=tuple(c.signals()),
        evidence=c.evidence(),
    )


def _canonical(pair: frozenset[ColumnRef], catalog: Catalog) -> frozenset[ColumnRef]:
    """Fill in schemas the query left implicit (``FROM orders``), when unambiguous."""
    resolved = []
    for schema, table, column in pair:
        info = catalog.find(schema, table)
        resolved.append((info.schema_name, info.name, column) if info else (schema, table, column))
    return frozenset(resolved)


def _calls(c: Candidate, joins: Counter[frozenset[ColumnRef]]) -> int:
    """Calls of statements joining every column pair of the candidate (min over pairs)."""
    counts = [
        joins.get(
            frozenset(
                {
                    (c.child.schema_name, c.child.name, child_col),
                    (c.parent.schema_name, c.parent.name, parent_col),
                }
            ),
            0,
        )
        for child_col, parent_col in zip(c.child_columns, c.parent_columns, strict=True)
    ]
    return min(counts) if counts else 0


def _latest_scan_time(session: Session, source: DataSource) -> datetime | None:
    """Discovery is stamped with the latest scan's time, so the lab's simulated calendar
    and real scans stay in order."""
    return session.scalar(
        select(IngestionRun.started_at)
        .where(IngestionRun.data_source_id == source.id)
        .order_by(IngestionRun.started_at.desc())
        .limit(1)
    )
