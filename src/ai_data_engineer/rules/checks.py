"""Running approved (``active``) business rules: plain SQL in the customer's database,
no AI. Each rule is one finding subject: opened when rows break it, resolved when none
do, and marked "not re-checked" when the rule can't run (a column was dropped, a query
timed out) instead of silently looking current.
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ai_data_engineer.detection.recording import (
    active_finding,
    latest_run_id,
    record_finding,
    resolve_finding,
)
from ai_data_engineer.discovery.catalog import Catalog, load_catalog
from ai_data_engineer.graph.models import (
    DataSource,
    FindingCategory,
    IngestionRun,
    Rule,
    RuleStatus,
    finding_fingerprint,
    utcnow,
)
from ai_data_engineer.ingestion.base import SourceAdapter
from ai_data_engineer.ingestion.scan import AdapterFactory, open_adapter
from ai_data_engineer.rules.settings import DEFAULT_RULE_SETTINGS, RuleSettings
from ai_data_engineer.rules.spec import describe, subject, validate
from ai_data_engineer.rules.store import rule_spec, rules_of

RULE_CHECK = "business_rule"


@dataclass
class RuleCheckResult:
    checked: int = 0
    opened: int = 0
    refreshed: int = 0
    resolved: int = 0
    not_rechecked: int = 0
    errors: dict[str, str] = field(default_factory=dict)  # rule id -> why it couldn't run

    def summary(self) -> str:
        text = (
            f"{self.checked} rules checked: +{self.opened} new, {self.refreshed} still broken, "
            f"-{self.resolved} resolved"
        )
        if self.not_rechecked:
            text += f", {self.not_rechecked} could not run"
        return text


def rule_fingerprint(rule: Rule) -> str:
    return finding_fingerprint(RULE_CHECK, rule.id)


def check_rules(
    session: Session,
    source: DataSource,
    *,
    adapter_factory: AdapterFactory = open_adapter,
    now: datetime | None = None,
    settings: RuleSettings = DEFAULT_RULE_SETTINGS,
) -> RuleCheckResult:
    """Run the source's active rules (``aide rules check``; a step of ``aide run``).
    Stamped with the latest scan's time, like discovery, so the lab calendar stays in order."""
    if not rules_of(session, source, RuleStatus.ACTIVE):
        return RuleCheckResult()  # nothing approved yet: don't even connect
    now = now or session.scalar(
        select(IngestionRun.started_at)
        .where(IngestionRun.data_source_id == source.id)
        .order_by(IngestionRun.started_at.desc())
        .limit(1)
    ) or utcnow()
    catalog = load_catalog(session, source)
    with adapter_factory(source) as adapter:
        return run_rule_checks(session, catalog, adapter, now, settings)


def run_rule_checks(
    session: Session,
    catalog: Catalog,
    adapter: SourceAdapter,
    now: datetime,
    settings: RuleSettings = DEFAULT_RULE_SETTINGS,
) -> RuleCheckResult:
    result = RuleCheckResult()
    source = catalog.source
    run_id = latest_run_id(session, source)
    for rule in rules_of(session, source, RuleStatus.ACTIVE):
        spec = rule_spec(rule)
        problems = validate(spec, catalog)
        if problems:
            _not_rechecked(session, rule, now, "; ".join(problems), result)
            continue
        table_ref, column_name = subject(spec)
        table = catalog.tables[table_ref]
        try:
            outcome = adapter.check_rule(
                spec, table.primary_key, table.estimated_rows, settings.sample_size
            )
        except DBAPIError as exc:
            reason = f"{type(exc.orig or exc).__name__}: {str(exc.orig or exc).splitlines()[0]}"
            _not_rechecked(session, rule, now, reason[:300], result)
            continue
        result.checked += 1
        fingerprint = rule_fingerprint(rule)
        if outcome.violating_rows == 0:
            if resolve_finding(session, source.tenant_id, fingerprint, now):
                result.resolved += 1
            continue
        fraction = outcome.violating_rows / outcome.checked_rows if outcome.checked_rows else 1.0
        rule_text = describe(spec)
        sampled = " (on a sample of the table)" if outcome.sampled else ""
        _, created = record_finding(
            session,
            source=source,
            fingerprint=fingerprint,
            category=FindingCategory.BUSINESS_RULE,
            check_name=RULE_CHECK,
            severity=settings.severity_high
            if fraction >= settings.high_severity_fraction
            else settings.severity_default,
            title=f"{outcome.violating_rows:,} rows in {table_ref} break the rule {rule_text}",
            description=(
                f"{outcome.violating_rows:,} of {outcome.checked_rows:,} rows ({fraction:.2%}) "
                f"in {table_ref} break the approved rule {rule_text}{sampled}. "
                f"Rule {str(rule.id)[:8]}, {rule.origin.value}-proposed"
                + (f", approved by {rule.reviewed_by}" if rule.reviewed_by else "")
                + "."
            ),
            evidence={
                "rule_id": str(rule.id),
                "rule": rule_text,
                "rule_spec": rule.definition["spec"],
                "violating_rows": outcome.violating_rows,
                "checked_rows": outcome.checked_rows,
                "violating_fraction": round(fraction, 6),
                "sampled": outcome.sampled,
                "sample_rows": outcome.sample,
                "sample_identifies_by": list(table.primary_key),
            },
            now=now,
            run_id=run_id,
            asset_key=table.asset_key,
            column_key=table.columns[column_name].column_key,
            rule_id=rule.id,
        )
        if created:
            result.opened += 1
        else:
            result.refreshed += 1
    return result


def _not_rechecked(
    session: Session, rule: Rule, now: datetime, reason: str, result: RuleCheckResult
) -> None:
    result.not_rechecked += 1
    result.errors[str(rule.id)] = reason
    finding = active_finding(session, rule.tenant_id, rule_fingerprint(rule))
    if finding is None:
        return
    finding.evidence = {
        **finding.evidence,
        "rechecked": False,
        "not_rechecked_at": now.isoformat(),
        "not_rechecked_reason": reason,
    }
    session.flush()
