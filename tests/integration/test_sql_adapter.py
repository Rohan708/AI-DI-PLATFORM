"""The generic SQL adapter (Stage 3).

1. On the Postgres lab it must measure and discover exactly what the tuned Postgres
   adapter does: same tables, row and NULL counts, distinct counts, relationships.
2. On a real MySQL (testcontainers) the whole engine works end to end: scan, discovery,
   orphans, structural detection, rules, row outliers, and read-only enforcement.
"""

import os
import random
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    create_engine,
    select,
    text,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ai_data_engineer.db import create_db_engine
from ai_data_engineer.detection.rows import ROW_OUTLIER, check_row_outliers
from ai_data_engineer.detection.runner import detect
from ai_data_engineer.discovery.catalog import load_catalog
from ai_data_engineer.discovery.discover import discover
from ai_data_engineer.graph.models import (
    AssetProfile,
    DataSource,
    Finding,
    RunStatus,
    SourceKind,
    Tenant,
)
from ai_data_engineer.graph.queries import get_asset_profile_history, get_column_profile_history
from ai_data_engineer.ingestion.base import KeyRef
from ai_data_engineer.ingestion.postgres import PostgresAdapter
from ai_data_engineer.ingestion.scan import scan_source
from ai_data_engineer.ingestion.settings import ScanSettings
from ai_data_engineer.ingestion.sql import SqlAdapter
from ai_data_engineer.lab.runner import build_lab
from ai_data_engineer.lab.scorer import load_relationships
from ai_data_engineer.rules.checks import check_rules
from ai_data_engineer.rules.spec import parse_spec
from ai_data_engineer.rules.store import add_user_rule

LAB = ScanSettings(exclude_schemas=("aide_lab",))
T1 = datetime(2026, 9, 15, 3, tzinfo=UTC)


# --- 1. the same results as the Postgres adapter, on the lab ---------------------------------


def _profiles(session: Session, source: DataSource) -> dict[str, tuple[Any, ...]]:
    out = {}
    for ref, table in load_catalog(session, source).tables.items():
        latest: list[AssetProfile] = get_asset_profile_history(session, table.asset_key, limit=1)
        rows = latest[0].row_count if latest else None
        for column in table.columns.values():
            p = get_column_profile_history(session, column.column_key, limit=1)
            out[f"{ref}.{column.name}"] = (
                rows,
                p[0].null_count if p else None,
                p[0].distinct_count if p else None,
                column.family,
            )
    return out


def test_generic_adapter_matches_the_postgres_adapter_on_the_lab(
    session: Session,
    make_source: Callable[[Tenant], DataSource],
    tenant: Tenant,
    make_database: Callable[[], str],
) -> None:
    url = make_database()
    lab = create_db_engine(url)
    build_lab(lab, seed=81, size="tiny")
    lab.dispose()
    tuned, generic = make_source(tenant), make_source(tenant)

    for source, factory in (
        (tuned, lambda _s: PostgresAdapter(url, LAB)),
        (generic, lambda _s: SqlAdapter(url, LAB)),
    ):
        scanned = scan_source(session, source, observed_at=T1, adapter_factory=factory)
        assert scanned.status is RunStatus.SUCCEEDED, scanned.errors
        discover(session, source, adapter_factory=factory)

    assert _profiles(session, generic) == _profiles(session, tuned)
    found = {(r.from_table, r.from_columns, r.to_table, r.to_columns)
             for r in load_relationships(session, generic.id)}  # fmt: skip
    expected = {(r.from_table, r.from_columns, r.to_table, r.to_columns)
                for r in load_relationships(session, tuned.id)}  # fmt: skip
    assert found == expected
    assert len(found) >= 10


# --- 2. end to end on MySQL ------------------------------------------------------------------


@pytest.fixture(scope="module")
def mysql_url() -> Iterator[str]:
    from testcontainers.community.mysql import MySqlContainer

    try:
        container = MySqlContainer("mysql:8.0", dialect="pymysql")
        container.start()
    except Exception as exc:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"MySQL container not available ({exc.__class__.__name__})")
    try:
        url = container.get_connection_url()
        _seed_shop(url)
        yield url
    finally:
        container.stop()


ORPHANS, FAT_FINGERS, SHIPPED_EARLY = 10, 3, 5


def _seed_shop(url: str) -> None:
    """A small shop: an undeclared link (orders.cust_no), a declared one (order_items),
    a table without a primary key, and planted problems."""
    md = MetaData()
    customers = Table(
        "customers", md, Column("id", Integer, primary_key=True), Column("email", String(120)),
        Column("created_at", DateTime),
    )  # fmt: skip
    orders = Table(
        "orders", md, Column("order_id", Integer, primary_key=True),
        Column("cust_no", Integer, nullable=False), Column("order_date", DateTime),
        Column("shipped_at", DateTime), Column("total_amount", Numeric(12, 2)),
    )  # fmt: skip
    items = Table(
        "order_items", md,
        Column("order_id", Integer, ForeignKey("orders.order_id"), primary_key=True),
        Column("line_no", Integer, primary_key=True), Column("quantity", Integer),
        Column("unit_price", Numeric(10, 2)),
    )  # fmt: skip
    audit = Table("audit_log", md, Column("event", String(40)), Column("at", DateTime))
    rng = random.Random(7)  # noqa: S311 - reproducible test data, not security
    start = datetime(2026, 1, 1, 9)  # noqa: DTZ001 - MySQL DATETIME has no time zone
    engine: Engine = create_engine(url)
    with engine.begin() as conn:
        md.create_all(conn)
        conn.execute(customers.insert(), [
            {"id": i, "email": f"user{i}@example.com", "created_at": start} for i in range(1, 201)
        ])  # fmt: skip
        order_rows, item_rows = [], []
        for n in range(1, 1001):
            day = start + timedelta(hours=n)
            lines = [(rng.randint(1, 3), Decimal(rng.randint(500, 20000)) / 100) for _ in "ab"]
            order_rows.append({
                "order_id": n, "cust_no": 99_999 if n <= ORPHANS else rng.randint(1, 200),
                "order_date": day,
                "shipped_at": day - timedelta(days=1) if n > 1000 - SHIPPED_EARLY
                else day + timedelta(days=1),
                "total_amount": sum(q * p for q, p in lines),
            })  # fmt: skip
            item_rows += [
                {"order_id": n, "line_no": i + 1, "quantity": q, "unit_price": p}
                for i, (q, p) in enumerate(lines)
            ]
        for row in item_rows[100:100 + FAT_FINGERS]:
            row["quantity"] = 500
        conn.execute(orders.insert(), order_rows)
        conn.execute(items.insert(), item_rows)
        conn.execute(audit.insert(), [{"event": "login", "at": start}] * 20)
        # Fill in InnoDB's row-count estimates (fresh tables report 0 until analysed).
        conn.execute(text("ANALYZE TABLE customers, orders, order_items, audit_log"))
        # The application's joins, so MySQL's statement digests show them.
        for _ in range(3):
            conn.execute(text(
                "SELECT o.order_id, c.email FROM orders o JOIN customers c ON o.cust_no = c.id "
                "WHERE o.order_id < 5"
            ))  # fmt: skip
    engine.dispose()


def _mysql_source(session: Session, tenant: Tenant) -> DataSource:
    source = DataSource(tenant_id=tenant.id, name="mysql-shop", kind=SourceKind.MYSQL)
    session.add(source)
    session.flush()
    return source


def test_the_engine_works_end_to_end_on_mysql(
    session: Session, tenant: Tenant, mysql_url: str
) -> None:
    source = _mysql_source(session, tenant)

    def factory(_source: DataSource) -> SqlAdapter:
        return SqlAdapter(mysql_url, ScanSettings())

    scanned = scan_source(session, source, observed_at=T1, adapter_factory=factory)
    assert scanned.status is RunStatus.SUCCEEDED, scanned.errors
    catalog = load_catalog(session, source)
    assert {t.split(".")[1] for t in catalog.tables} == {
        "customers", "orders", "order_items", "audit_log",
    }  # fmt: skip
    orders = next(t for t in catalog.tables.values() if t.name == "orders")
    assert orders.estimated_rows is not None
    assert orders.columns["cust_no"].distinct_count is not None

    discover(session, source, adapter_factory=factory)
    links = {(r.from_table.split(".")[1], r.from_columns, r.to_table.split(".")[1], r.to_columns)
             for r in load_relationships(session, source.id)}  # fmt: skip
    assert ("orders", ("cust_no",), "customers", ("id",)) in links  # undeclared, discovered
    assert ("order_items", ("order_id",), "orders", ("order_id",)) in links  # declared FK

    with factory(source) as adapter:
        schema = orders.schema_name
        orphans = adapter.count_orphans(
            KeyRef(schema, "orders", ("cust_no",), 1000), KeyRef(schema, "customers", ("id",)),
            row_ids=("order_id",), sample_size=5,
        )  # fmt: skip
        assert (orphans.orphan_rows, len(orphans.sample)) == (ORPHANS, 5)
        with pytest.raises(DBAPIError), adapter.read_only() as conn:  # the session is read-only
            conn.execute(text("INSERT INTO audit_log (event) VALUES ('aide was here')"))

    result = detect(session, source)
    assert result.by_check["missing_primary_key"].opened == 1  # audit_log

    add_user_rule(
        session, source, catalog,
        parse_spec({"kind": "compare_columns", "table": f"{schema}.orders",
                    "column": "shipped_at", "op": ">=", "other_column": "order_date"}),
        "tester", T1,
    )  # fmt: skip
    checked = check_rules(session, source, adapter_factory=factory)
    assert checked.opened == 1, checked.errors
    rule_finding = session.scalars(
        select(Finding).where(Finding.data_source_id == source.id,
                              Finding.check_name == "business_rule")
    ).one()  # fmt: skip
    assert rule_finding.evidence["violating_rows"] == SHIPPED_EARLY

    outliers = check_row_outliers(session, source, adapter_factory=factory, now=T1)
    assert outliers.errors == {}
    found = session.scalars(
        select(Finding).where(
            Finding.data_source_id == source.id, Finding.check_name == ROW_OUTLIER
        )
    ).all()
    assert [f.evidence["outlier_rows"] for f in found] == [FAT_FINGERS]  # quantity only
