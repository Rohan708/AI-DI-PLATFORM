"""The generic adapter's per-database parts, without any database: type mapping, value
formatting, and that every query compiles for MySQL, SQL Server and Oracle (their
SQLAlchemy dialects ship with SQLAlchemy, no driver needed)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy import types as sqltypes
from sqlalchemy.dialects import mssql, mysql, oracle, postgresql
from sqlalchemy.engine import Dialect

from ai_data_engineer.graph.models import TypeFamily
from ai_data_engineer.ingestion.settings import ScanSettings
from ai_data_engineer.ingestion.sql.dialects import (
    PROFILES,
    UnsupportedDialectError,
    profile_for,
)
from ai_data_engineer.ingestion.sql.queries import _limited, as_text, rule_conditions, rule_rows
from ai_data_engineer.ingestion.sql.types import type_family
from ai_data_engineer.rules.spec import parse_spec

DIALECTS: dict[str, Dialect] = {
    "mysql": mysql.dialect(),
    "mssql": mssql.dialect(),
    "oracle": oracle.dialect(),
    "postgresql": postgresql.dialect(),
}


@pytest.mark.parametrize(
    ("column_type", "dialect", "family"),
    [
        (mysql.TINYINT(), "mysql", TypeFamily.INTEGER),
        (mysql.DECIMAL(12, 2), "mysql", TypeFamily.DECIMAL),
        (mysql.DOUBLE(), "mysql", TypeFamily.FLOAT),
        (mysql.DATETIME(), "mysql", TypeFamily.TIMESTAMP),
        (mysql.TEXT(), "mysql", TypeFamily.STRING),
        (mysql.VARBINARY(16), "mysql", TypeFamily.BINARY),
        (mssql.BIT(), "mssql", TypeFamily.BOOLEAN),
        (mssql.NVARCHAR(50), "mssql", TypeFamily.STRING),
        (mssql.NTEXT(), "mssql", TypeFamily.OTHER),  # can't be compared or counted distinct
        (mssql.DATETIME2(), "mssql", TypeFamily.TIMESTAMP),
        (oracle.NUMBER(10, 0), "oracle", TypeFamily.INTEGER),
        (oracle.NUMBER(12, 2), "oracle", TypeFamily.DECIMAL),
        (oracle.CLOB(), "oracle", TypeFamily.OTHER),
        (oracle.DATE(), "oracle", TypeFamily.TIMESTAMP),  # Oracle DATE carries a time
        (sqltypes.NullType(), "mysql", TypeFamily.OTHER),
    ],
)
def test_type_families(column_type: Any, dialect: str, family: TypeFamily) -> None:
    assert type_family(column_type, DIALECTS[dialect]) is family


def test_values_print_like_postgres() -> None:
    assert as_text(datetime(2026, 3, 15, 9, 0, tzinfo=UTC)) == "2026-03-15 09:00:00+00:00"
    assert as_text(date(2026, 3, 15)) == "2026-03-15"
    assert as_text(Decimal("12.50")) == "12.50"
    assert as_text(True) == "true"
    assert as_text(b"\x00\x01") is None  # binary is never stored


def test_dialect_profiles() -> None:
    assert set(PROFILES) == {"postgresql", "mysql", "mssql", "oracle", "snowflake"}
    with pytest.raises(UnsupportedDialectError):
        profile_for("db2")
    statements = profile_for("mysql").session_sql(
        ScanSettings(statement_timeout_ms=5000, lock_timeout_ms=1500)
    )
    assert "SET SESSION TRANSACTION READ ONLY" in statements
    assert "SET SESSION max_execution_time = 5000" in statements
    assert "SET SESSION innodb_lock_wait_timeout = 1" in statements
    assert profile_for("oracle").transaction_sql(ScanSettings()) == ["SET TRANSACTION READ ONLY"]


RULES: list[dict[str, Any]] = [
    {"kind": "compare_columns", "table": "shop.shipments", "column": "shipped_at", "op": ">=",
     "other_column": "order_date",
     "via": {"parent_table": "shop.orders", "on": [["ord_id", "order_id"]]}},
    {"kind": "compare_constant", "table": "shop.order_items", "column": "quantity", "op": ">",
     "value": 0},
    {"kind": "sum_matches", "table": "legacy.INV_HDR", "column": "TOTAL_AMT",
     "child_table": "legacy.INV_LINE", "child_column": "LINE_AMT", "on": [["INV_NO", "INV_NO"]]},
]  # fmt: skip


@pytest.mark.parametrize("dialect", ["mysql", "mssql", "oracle", "postgresql"])
@pytest.mark.parametrize("rule", RULES, ids=[r["kind"] for r in RULES])
def test_rules_compile_for_every_dialect(rule: dict[str, Any], dialect: str) -> None:
    spec = parse_spec(rule)
    rows, labels = rule_rows(spec, ("id",))
    s, sampled = _limited(rows, 5_000_000, 1_000_000, 1_000_000)
    judged, broken = rule_conditions(spec, s)
    query = select(func.sum(judged.cast(sqltypes.Integer)), func.count()).select_from(s)
    sql = str(query.compile(dialect=DIALECTS[dialect]))
    assert sampled
    assert labels == ("aide_id_0",)
    limit_words = {
        "mysql": ("LIMIT",), "mssql": ("TOP",), "oracle": ("FETCH FIRST", "ROWNUM"),
        "postgresql": ("LIMIT",),
    }  # fmt: skip
    assert any(word in sql for word in limit_words[dialect])
    assert str(broken.compile(dialect=DIALECTS[dialect]))
