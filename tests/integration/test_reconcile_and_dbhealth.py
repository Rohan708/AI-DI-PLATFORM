"""Stage 3.2 and 3.3 against real Postgres databases.

Reconciliation: two identical copies of the lab agree; after rows go missing and NULLs
appear in the copy, exactly those differences are reported.
Database health: a nearly exhausted sequence, an unused index and a session stuck in a
transaction are found, and resolve once fixed.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from ai_data_engineer.db import create_db_engine
from ai_data_engineer.detection.dbhealth import DbHealthSettings, check_db_health
from ai_data_engineer.graph.models import DataSource, Finding, FindingStatus, Tenant
from ai_data_engineer.ingestion.postgres import PostgresAdapter
from ai_data_engineer.ingestion.scan import scan_source
from ai_data_engineer.ingestion.settings import ScanSettings
from ai_data_engineer.lab.runner import build_lab
from ai_data_engineer.reconcile.run import add_pair, reconcile

SETTINGS = ScanSettings(exclude_schemas=("aide_lab",))
T1 = datetime(2026, 9, 20, 3, tzinfo=UTC)
MISSING_ORDERS = 7


def _factory(url: str) -> Callable[[DataSource], Any]:
    return lambda _source: PostgresAdapter(url, SETTINGS)


def _lab(make_database: Callable[[], str], seed: int) -> str:
    url = make_database()
    engine = create_db_engine(url)
    build_lab(engine, seed=seed, size="tiny")
    engine.dispose()
    return url


def test_reconciliation_reports_exactly_what_the_copy_lost(
    session: Session,
    make_source: Callable[[Tenant], DataSource],
    tenant: Tenant,
    make_database: Callable[[], str],
) -> None:
    app_url, copy_url = _lab(make_database, seed=91), _lab(make_database, seed=91)
    app, copy = make_source(tenant), make_source(tenant)
    for source, url in ((app, app_url), (copy, copy_url)):
        scan_source(session, source, observed_at=T1, adapter_factory=_factory(url))
    pair = add_pair(session, "app-vs-copy", app, copy, {})

    same = reconcile(session, pair, now=T1)
    assert same.tables_compared >= 10
    assert (same.opened, same.unmatched) == (0, [])  # identical copies: nothing to report

    engine = create_db_engine(copy_url)
    with engine.begin() as conn:  # the copy's load lost some orders and some postal codes
        lost = "SELECT order_id FROM shop.orders ORDER BY order_id DESC LIMIT :n"
        conn.execute(text(f"DELETE FROM shop.order_items WHERE order_id IN ({lost})"),  # noqa: S608
                     {"n": MISSING_ORDERS})  # fmt: skip
        conn.execute(text(f"DELETE FROM shop.orders WHERE order_id IN ({lost})"),  # noqa: S608
                     {"n": MISSING_ORDERS})  # fmt: skip
        conn.execute(text("UPDATE shop.addresses SET postal_code = NULL WHERE id % 10 = 0"))
    engine.dispose()
    scan_source(session, copy, observed_at=T1, adapter_factory=_factory(copy_url))

    differs = reconcile(session, pair, now=T1)
    findings = session.scalars(select(Finding).where(Finding.data_source_id == app.id)).all()
    by_check = {(f.check_name, f.evidence["left_table"], f.evidence["column"]) for f in findings}
    assert ("reconcile_row_count", "shop.orders", None) in by_check
    assert ("reconcile_row_count", "shop.order_items", None) in by_check
    assert ("reconcile_null_rate", "shop.addresses", "postal_code") in by_check
    orders = next(f for f in findings if f.evidence["left_table"] == "shop.orders")
    assert orders.evidence["left_value"] - orders.evidence["right_value"] == MISSING_ORDERS
    assert f"{MISSING_ORDERS} fewer rows" in orders.title
    # payments still point at the lost orders, but the payments table itself is intact
    assert not any(f.evidence["left_table"] == "shop.payments" for f in findings)
    assert differs.opened == len(findings)


def test_database_health_findings_appear_and_resolve(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url = _lab(make_database, seed=92)
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text("CREATE SEQUENCE public.ticket_no MAXVALUE 1000"))
        conn.execute(text("SELECT setval('public.ticket_no', 950)"))
        conn.execute(text("CREATE INDEX ix_orders_channel_unused ON shop.orders (channel)"))
    stuck = admin.connect()
    stuck.execute(text("SELECT 1"))  # opens a transaction and leaves it idle
    scan_source(session, source, observed_at=T1, adapter_factory=_factory(url))
    eager = DbHealthSettings(
        unused_index_min_bytes=0, unused_index_min_stats_days=0, idle_transaction_min_seconds=0
    )

    found = check_db_health(session, source, adapter_factory=_factory(url), settings=eager)

    assert found.checks_run  # Postgres has readings
    by_check = {
        f.check_name: f
        for f in session.scalars(select(Finding).where(Finding.data_source_id == source.id))
    }
    sequence = by_check["db_sequence_exhaustion"]
    assert sequence.evidence["used"] == 0.95
    assert "95%" in sequence.title
    assert any(
        f.check_name == "db_unused_index" and f.evidence["subject"].endswith("unused")
        for f in session.scalars(select(Finding).where(Finding.data_source_id == source.id))
    )
    assert by_check["db_idle_in_transaction"].evidence["sessions"] >= 1

    stuck.close()  # the application finishes its transaction
    with admin.begin() as conn:
        conn.execute(text("ALTER SEQUENCE public.ticket_no MAXVALUE 100000"))
    admin.dispose()
    fixed = check_db_health(session, source, adapter_factory=_factory(url), settings=eager)

    assert fixed.resolved >= 2
    assert by_check["db_sequence_exhaustion"].status is FindingStatus.RESOLVED
    assert by_check["db_idle_in_transaction"].status is FindingStatus.RESOLVED
