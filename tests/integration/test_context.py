"""Stage 2.7 against the real lab: answers to the engine's questions catch planted
problems from the very first scan (learned checks need a week of history), deadlines
flag late tables (closed days excused), criticality weighs in health, and dbt's
schema.yml arrives as rules and confirmed relationships."""

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lumida.context.dbt import import_dbt
from lumida.context.questions import answer, generate
from lumida.context.store import set_note
from lumida.db import create_db_engine
from lumida.detection.checks.timeseries import FRESHNESS_DEADLINE
from lumida.detection.runner import detect
from lumida.discovery.catalog import load_catalog
from lumida.discovery.discover import discover
from lumida.discovery.store import current_relationships
from lumida.graph.models import (
    DataSource,
    Finding,
    Origin,
    RelationshipStatus,
    Rule,
    RuleStatus,
)
from lumida.health_score.engine import compute_health
from lumida.ingestion.postgres import PostgresAdapter
from lumida.ingestion.scan import scan_source
from lumida.ingestion.settings import ScanSettings
from lumida.lab.runner import build_lab, inject, lab_scan_time
from lumida.lab.scorer import load_relationships
from lumida.rules.checks import check_rules

SETTINGS = ScanSettings(exclude_schemas=("aide_lab",))


def _factory(url: str) -> Callable[[DataSource], Any]:
    return lambda _source: PostgresAdapter(url, SETTINGS)


def _lab(session: Session, source: DataSource, make_database: Callable[[], str], seed: int) -> Any:
    url = make_database()
    lab = create_db_engine(url)
    state = build_lab(lab, seed=seed, size="tiny")
    first_scan = lab_scan_time(state.current_day)
    scan_source(session, source, observed_at=first_scan, adapter_factory=_factory(url))
    discover(session, source, adapter_factory=_factory(url))
    return url, lab, first_scan


def test_answers_catch_planted_problems_from_the_first_scan(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url, lab, t1 = _lab(session, source, make_database, seed=97)
    questions = generate(session, source, now=t1, limit=500)
    kinds = {q.kind for q in questions}
    assert {"closed_weekdays", "unique", "required", "allowed", "criticality"} <= kinds
    by = {(q.kind, q.data.get("table"), q.data.get("column")): q for q in questions}
    postal = by[("required", "shop.addresses", "postal_code")]
    country = by[("allowed", "shop.addresses", "country")]
    assert "US" in country.data["values"]

    assert answer(session, source, postal, "yes", by="tester", now=t1).startswith("rule added")
    assert answer(session, source, country, "yes", by="tester", now=t1).startswith("rule added")
    answer(session, source, by[("closed_weekdays", None, None)], "none", by="tester", now=t1)
    remaining = {q.key for q in generate(session, source, now=t1, limit=500)}
    assert postal.key not in remaining  # answered questions aren't asked again
    rules = session.scalars(select(Rule).where(Rule.data_source_id == source.id)).all()
    assert {(r.origin, r.status, r.rule_type) for r in rules} == {
        (Origin.USER, RuleStatus.ACTIVE, "not_null"),
        (Origin.USER, RuleStatus.ACTIVE, "allowed_values"),
    }

    assert check_rules(session, source, adapter_factory=_factory(url)).opened == 0  # clean
    planted = {a.scenario: a for a in inject(lab, ["null_spike", "country_variants"])}
    lab.dispose()
    caught = check_rules(session, source, adapter_factory=_factory(url))
    assert caught.opened == 2, caught.errors
    found = {f.evidence["rule_spec"]["kind"]: f for f in session.scalars(
        select(Finding).where(Finding.data_source_id == source.id,
                              Finding.check_name == "business_rule"))}  # fmt: skip
    assert found["not_null"].evidence["violating_rows"] == planted["null_spike"].details["rows"]
    assert (
        found["allowed_values"].evidence["violating_rows"]
        == planted["country_variants"].details["rows"]
    )

    # The learned check needs history: after a second scan it is still learning.
    scan_source(session, source, observed_at=t1 + timedelta(days=1), adapter_factory=_factory(url))
    learned = detect(session, source)
    assert learned.by_check["null_rate_spike"].opened == 0
    assert learned.by_check["inconsistent_categories"].opened == 0


def test_deadlines_flag_late_tables_and_excuse_closed_days(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url, lab, t1 = _lab(session, source, make_database, seed=98)
    lab.dispose()
    catalog = load_catalog(session, source)
    daily = catalog.tables["reporting.daily_sales"]
    set_note(session, source, "freshness", {"column": "loaded_at", "max_age_hours": 26},
             by="tester", now=t1, asset_key=daily.asset_key)  # fmt: skip

    assert detect(session, source).by_check[FRESHNESS_DEADLINE].opened == 0  # loaded 1 h ago

    later = t1 + timedelta(days=3)  # three days with no load
    scan_source(session, source, observed_at=later, adapter_factory=_factory(url))
    late = detect(session, source)
    assert late.by_check[FRESHNESS_DEADLINE].opened == 1
    finding = session.scalars(select(Finding).where(Finding.check_name == FRESHNESS_DEADLINE)).one()
    assert finding.evidence["deadline_hours"] == 26
    assert finding.evidence["age_hours"] > 26

    # If the business was closed on the missing days, it isn't late after all.
    skipped = sorted({(t1 + timedelta(days=d)).weekday() for d in (0, 1, 2)})
    set_note(session, source, "closed_weekdays", {"days": skipped}, by="tester", now=later)
    excused = detect(session, source)
    assert excused.by_check[FRESHNESS_DEADLINE].resolved == 1

    # Criticality: the same open findings weigh more on a critical table.
    before = compute_health(session, source, later).source.score
    worst = min(compute_health(session, source, later).tables, key=lambda t: t.score.score)
    set_note(session, source, "criticality", {"level": 3}, by="tester", now=later,
             asset_key=worst.asset_key)  # fmt: skip
    assert compute_health(session, source, later).source.score < before


def test_dbt_schema_yml_becomes_rules_and_confirmed_links(
    session: Session, source: DataSource, make_database: Callable[[], str], tmp_path: Path
) -> None:
    _, lab, t1 = _lab(session, source, make_database, seed=99)
    lab.dispose()
    schema_yml = tmp_path / "schema.yml"
    schema_yml.write_text(
        """
version: 2
models:
  - name: customers
    description: One row per registered customer.
    columns:
      - name: customer_code
        description: The customer's business code, shared with billing.
        data_tests: [unique, not_null]
  - name: addresses
    columns:
      - name: country
        tests:
          - accepted_values:
              values: ['US', 'DE', 'GB', 'FR', 'CA', 'IN', 'NL', 'ES', 'IT', 'AU']
  - name: orders
    columns:
      - name: cust_no
        tests:
          - relationships:
              to: ref('customers')
              field: id
  - name: no_such_model
""",
        encoding="utf-8",
    )

    result = import_dbt(session, source, schema_yml, by="tester", now=t1, schema="shop")

    assert result.tables_unmatched == ["no_such_model"]
    assert result.descriptions == 2
    assert len(result.rules_added) == 3
    assert result.links_confirmed == ["shop.orders.cust_no -> customers.id"]
    links = {(r.from_table, r.from_columns): r for r in load_relationships(session, source.id)}
    assert ("shop.orders", ("cust_no",)) in links
    confirmed = [
        r for r in session.scalars(select(Rule).where(Rule.data_source_id == source.id))
        if r.created_by == "dbt:schema.yml"
    ]  # fmt: skip
    assert len(confirmed) == 3
    statuses = {r.status for r in current_relationships(session, load_catalog(session, source))
                if r.status is RelationshipStatus.CONFIRMED}  # fmt: skip
    assert statuses
    again = import_dbt(session, source, schema_yml, by="tester", now=t1, schema="shop")
    assert (len(again.rules_added), again.rules_existing) == (0, 3)
