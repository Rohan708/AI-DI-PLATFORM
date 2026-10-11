"""Stage 2.7 without a database: parsing answers, closed-day arithmetic, weighted health,
the new rule kinds and their SQL for every dialect, and reading dbt schema.yml."""

from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy import types as sqltypes
from sqlalchemy.dialects import mssql, mysql, oracle, postgresql

from ai_data_engineer.context.dbt import parse_dbt
from ai_data_engineer.context.questions import AnswerError, _yes
from ai_data_engineer.context.store import ContextError, parse_criticality, parse_weekdays
from ai_data_engineer.detection.checks.timeseries import _closed_days_between
from ai_data_engineer.health_score.score import Score, group_score
from ai_data_engineer.ingestion.postgres.rules import compile_rule
from ai_data_engineer.ingestion.sql.queries import _limited, rule_conditions, rule_rows
from ai_data_engineer.rules.spec import describe, parse_spec

NOT_NULL = {"kind": "not_null", "table": "shop.addresses", "column": "postal_code"}
UNIQUE = {"kind": "unique", "table": "shop.customers", "column": "email"}
ALLOWED = {"kind": "allowed_values", "table": "shop.addresses", "column": "country",
           "values": ["US", "DE", "GB"]}  # fmt: skip


def test_answers_are_parsed() -> None:
    assert parse_weekdays("sat, sun") == [5, 6]
    assert parse_weekdays("Sunday") == [6]
    assert parse_weekdays("none") == []
    with pytest.raises(ContextError):
        parse_weekdays("funday")
    assert parse_criticality("critical") == 3
    assert parse_criticality("1") == 1
    with pytest.raises(ContextError):
        parse_criticality("very")
    assert _yes("Y") is True
    assert _yes("no") is False
    with pytest.raises(AnswerError):
        _yes("maybe")


def test_closed_days_between() -> None:
    friday_noon, monday_3am = datetime(2026, 3, 13, 12), datetime(2026, 3, 16, 3)  # noqa: DTZ001
    assert _closed_days_between(friday_noon, monday_3am, frozenset({5, 6})) == 2  # sat + sun
    assert _closed_days_between(friday_noon, monday_3am, frozenset()) == 0
    assert _closed_days_between(monday_3am, friday_noon, frozenset({5, 6})) == 0


def test_critical_tables_weigh_more_in_the_average() -> None:
    healthy, broken = Score(100.0), Score(40.0)
    assert group_score([healthy, broken]).score == 70.0
    assert group_score([healthy, broken], [1.0, 2.0]).score == 60.0  # the broken one is critical
    assert group_score([healthy, broken], [1.0, 2.0]).min_child == 40.0


def test_new_rule_kinds() -> None:
    assert describe(parse_spec(NOT_NULL)) == "shop.addresses.postal_code is always filled"
    assert describe(parse_spec(UNIQUE)) == "shop.customers.email is unique"
    assert describe(parse_spec(ALLOWED)) == "shop.addresses.country in (US, DE, GB)"
    pg = compile_rule(parse_spec(ALLOWED), '"shop"."addresses" AS t')
    assert pg.params == {"allowed": ["US", "DE", "GB"]}
    assert "ANY(CAST(:allowed AS text[]))" in pg.broken
    assert (
        "HAVING count(*) > 1" in compile_rule(parse_spec(UNIQUE), '"shop"."customers" AS t').broken
    )


@pytest.mark.parametrize("dialect", [mysql.dialect(), mssql.dialect(), oracle.dialect(),
                                     postgresql.dialect()])  # fmt: skip
@pytest.mark.parametrize("rule", [NOT_NULL, UNIQUE, ALLOWED], ids=lambda r: r["kind"])
def test_new_kinds_compile_for_every_dialect(rule: dict[str, object], dialect: object) -> None:
    spec = parse_spec(rule)
    rows, _ = rule_rows(spec, ("id",))
    s, _ = _limited(rows, None, 1, 1)
    _, broken = rule_conditions(spec, s)
    query = select(func.sum(broken.cast(sqltypes.Integer))).select_from(s)
    assert str(query.compile(dialect=dialect))  # type: ignore[arg-type]


SCHEMA_YML = """
version: 2
models:
  - name: customers
    description: One row per registered customer.
    columns:
      - name: email
        description: Login and contact address.
        data_tests: [unique, not_null]
      - name: status
        tests:
          - accepted_values:
              values: ['active', 'closed']
  - name: orders
    columns:
      - name: cust_no
        tests:
          - relationships:
              arguments:
                to: ref('customers')
                field: id
sources:
  - name: legacy
    tables:
      - name: CUST_MASTER
        description: The old billing system's customers.
"""


def test_dbt_schema_yml_is_read() -> None:
    models = {m.name: m for m in parse_dbt(SCHEMA_YML)}
    assert set(models) == {"customers", "orders", "CUST_MASTER"}
    customers = models["customers"]
    assert customers.description == "One row per registered customer."
    email = customers.columns[0]
    assert [name for name, _ in email.tests] == ["unique", "not_null"]
    status = customers.columns[1]
    assert status.tests == [("accepted_values", {"values": ["active", "closed"]})]
    (relationship,) = models["orders"].columns[0].tests
    assert relationship[0] == "relationships"
    assert relationship[1]["to"] == "ref('customers')"
    assert relationship[1]["field"] == "id"
