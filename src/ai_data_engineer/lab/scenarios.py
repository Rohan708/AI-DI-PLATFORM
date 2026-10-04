"""The anomaly injector: a library of ways to break the lab database.

Each scenario changes the lab data (or schedules an ETL fault for the next simulated day)
and returns the answer-key entries describing exactly what it broke. Row selection is
deterministic (ordered by a hash of the key and the lab seed), so injections are
reproducible too.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Connection, text

from ai_data_engineer.graph.models import FindingCategory
from ai_data_engineer.lab.answer_key import ExpectedAnomaly
from ai_data_engineer.lab.state import LabState

# How hard each scenario hits (fractions of rows affected).
NULL_SPIKE_FRACTION = 0.08
COUNTRY_VARIANT_FRACTION = 0.2
NEGATIVE_QUANTITY_FRACTION = 0.005
ORPHAN_FRACTION = 0.02
ORPHAN_ID_OFFSET = 10_000_000  # far beyond any real customer id
DUPLICATE_CUSTOMER_FRACTION = 0.03
SHIP_BEFORE_ORDER_FRACTION = 0.01
INVOICE_MISMATCH_FRACTION = 0.01
INVOICE_MISMATCH_FACTOR = 1.1
HALF_LOAD_VOLUME_FACTOR = 0.4

Apply = Callable[[Connection, LabState], list[ExpectedAnomaly]]


@dataclass(frozen=True)
class Scenario:
    name: str
    category: FindingCategory
    summary: str
    apply: Apply


SCENARIOS: dict[str, Scenario] = {}


def _scenario(name: str, category: FindingCategory, summary: str) -> Callable[[Apply], Apply]:
    def register(fn: Apply) -> Apply:
        if name in SCENARIOS:
            raise ValueError(f"duplicate scenario {name!r}")
        SCENARIOS[name] = Scenario(name, category, summary, fn)
        return fn

    return register


def _effective(state: LabState) -> date:
    """Injected problems show up in the scan after the next simulated day."""
    return state.current_day + timedelta(days=1)


def _sample_size(conn: Connection, count_sql: str, fraction: float) -> int:
    total = conn.scalar(text(count_sql)) or 0
    return max(1, math.ceil(total * fraction))


def _expect(
    state: LabState,
    scenario: str,
    category: FindingCategory,
    table: str,
    column: str | None,
    check_hint: str,
    stage: str,
    description: str,
    details: dict[str, Any],
    related: tuple[str, ...] = (),
) -> ExpectedAnomaly:
    return ExpectedAnomaly(
        scenario=scenario,
        category=category,
        table=table,
        column=column,
        check_hint=check_hint,
        detect_stage=stage,
        description=description,
        effective_day=_effective(state),
        details=details,
        related_tables=related,
    )


# --- structural ------------------------------------------------------------------------


@_scenario("drop_column", FindingCategory.STRUCTURAL, "A column disappears from a table")
def drop_column(conn: Connection, state: LabState) -> list[ExpectedAnomaly]:
    conn.execute(text("ALTER TABLE shop.products DROP COLUMN weight_grams"))
    return [
        _expect(
            state, "drop_column", FindingCategory.STRUCTURAL, "shop.products", "weight_grams",
            "schema_drift_column_removed", "1.5",
            "Column weight_grams was dropped from shop.products", {},
        )
    ]  # fmt: skip


@_scenario("widen_column_type", FindingCategory.STRUCTURAL, "A column's type silently changes")
def widen_column_type(conn: Connection, state: LabState) -> list[ExpectedAnomaly]:
    conn.execute(text("ALTER TABLE shop.orders ALTER COLUMN channel TYPE varchar(50)"))
    return [
        _expect(
            state, "widen_column_type", FindingCategory.STRUCTURAL, "shop.orders", "channel",
            "type_drift", "1.5", "shop.orders.channel changed from varchar(20) to varchar(50)",
            {"before": "character varying(20)", "after": "character varying(50)"},
        )
    ]  # fmt: skip


@_scenario("drop_primary_key", FindingCategory.STRUCTURAL, "A table loses its primary key")
def drop_primary_key(conn: Connection, state: LabState) -> list[ExpectedAnomaly]:
    conn.execute(text('ALTER TABLE legacy."INV_LINE" DROP CONSTRAINT "INV_LINE_PK"'))
    return [
        _expect(
            state, "drop_primary_key", FindingCategory.STRUCTURAL, "legacy.INV_LINE", None,
            "primary_key_removed", "1.5",
            'legacy.INV_LINE lost its primary key ("INV_NO", "LINE_NO")', {},
        )
    ]  # fmt: skip


# --- relational ------------------------------------------------------------------------


@_scenario(
    "orphan_orders", FindingCategory.RELATIONAL, "Orders point to customers that don't exist"
)
def orphan_orders(conn: Connection, state: LabState) -> list[ExpectedAnomaly]:
    n = _sample_size(conn, "SELECT count(*) FROM shop.orders", ORPHAN_FRACTION)
    affected = conn.execute(
        text(
            "UPDATE shop.orders SET cust_no = cust_no + :offset WHERE order_id IN ("
            "SELECT order_id FROM shop.orders ORDER BY md5(order_id::text || :salt) LIMIT :n)"
        ),
        {"offset": ORPHAN_ID_OFFSET, "salt": str(state.seed), "n": n},
    ).rowcount
    return [
        _expect(
            state, "orphan_orders", FindingCategory.RELATIONAL, "shop.orders", "cust_no",
            "orphan_rows", "1.4",
            f"{affected} orders reference customer ids that don't exist in shop.customers",
            {"rows": affected, "fraction": ORPHAN_FRACTION},
            related=("reporting.customer_summary",),
        )
    ]  # fmt: skip


@_scenario("duplicate_customers", FindingCategory.RELATIONAL, "The same people registered twice")
def duplicate_customers(conn: Connection, state: LabState) -> list[ExpectedAnomaly]:
    n = _sample_size(conn, "SELECT count(*) FROM shop.customers", DUPLICATE_CUSTOMER_FRACTION)
    originals = conn.execute(
        text(
            "SELECT first_name, middle_name, last_name, email, phone, created_at "
            "FROM shop.customers ORDER BY md5(id::text || :salt) LIMIT :n"
        ),
        {"salt": str(state.seed), "n": n},
    ).all()
    rows = []
    for original in originals:
        new_id = state.take("customer")
        rows.append(
            {
                "id": new_id,
                "code": f"CU{new_id:08d}",
                "first": original.first_name,
                "middle": original.middle_name,
                "last": original.last_name,
                # Same person, same mailbox, different casing: a classic duplicate.
                "email": original.email.upper() if original.email else None,
                "phone": original.phone,
                "created_at": original.created_at,
            }
        )
    conn.execute(
        text(
            "INSERT INTO shop.customers (id, customer_code, first_name, middle_name, last_name, "
            "email, phone, created_at) VALUES (:id, :code, :first, :middle, :last, :email, "
            ":phone, :created_at)"
        ),
        rows,
    )
    return [
        _expect(
            state, "duplicate_customers", FindingCategory.RELATIONAL, "shop.customers", "email",
            "duplicate_entities", "1.5",
            f"{len(rows)} customers were duplicated (same name, email differs only by case)",
            {"rows": len(rows), "fraction": DUPLICATE_CUSTOMER_FRACTION},
        )
    ]  # fmt: skip


# --- column values ---------------------------------------------------------------------


@_scenario(
    "null_spike", FindingCategory.COLUMN_VALUE, "A normally complete column fills with NULLs"
)
def null_spike(conn: Connection, state: LabState) -> list[ExpectedAnomaly]:
    n = _sample_size(conn, "SELECT count(*) FROM shop.addresses", NULL_SPIKE_FRACTION)
    affected = conn.execute(
        text(
            "UPDATE shop.addresses SET postal_code = NULL WHERE id IN ("
            "SELECT id FROM shop.addresses WHERE postal_code IS NOT NULL "
            "ORDER BY md5(id::text || :salt) LIMIT :n)"
        ),
        {"salt": str(state.seed), "n": n},
    ).rowcount
    return [
        _expect(
            state, "null_spike", FindingCategory.COLUMN_VALUE, "shop.addresses", "postal_code",
            "null_rate_spike", "1.5",
            f"postal_code set to NULL on {affected} addresses (normally 0% NULL)",
            {"rows": affected, "fraction": NULL_SPIKE_FRACTION},
        )
    ]  # fmt: skip


@_scenario(
    "country_variants",
    FindingCategory.COLUMN_VALUE,
    "'US' starts appearing as 'USA'/'United States'",
)
def country_variants(conn: Connection, state: LabState) -> list[ExpectedAnomaly]:
    n = _sample_size(
        conn, "SELECT count(*) FROM shop.addresses WHERE country = 'US'", COUNTRY_VARIANT_FRACTION
    )
    affected = conn.execute(
        text(
            "UPDATE shop.addresses SET country = CASE WHEN id % 2 = 0 THEN 'USA' "
            "ELSE 'United States' END WHERE id IN (SELECT id FROM shop.addresses "
            "WHERE country = 'US' ORDER BY md5(id::text || :salt) LIMIT :n)"
        ),
        {"salt": str(state.seed), "n": n},
    ).rowcount
    return [
        _expect(
            state, "country_variants", FindingCategory.COLUMN_VALUE, "shop.addresses", "country",
            "inconsistent_categories", "1.5",
            f"{affected} 'US' values rewritten as 'USA' / 'United States'",
            {"rows": affected, "new_values": ["USA", "United States"]},
        )
    ]  # fmt: skip


@_scenario("negative_quantity", FindingCategory.COLUMN_VALUE, "Impossible negative quantities")
def negative_quantity(conn: Connection, state: LabState) -> list[ExpectedAnomaly]:
    n = _sample_size(conn, "SELECT count(*) FROM shop.order_items", NEGATIVE_QUANTITY_FRACTION)
    affected = conn.execute(
        text(
            "UPDATE shop.order_items SET quantity = -quantity WHERE (order_id, line_no) IN ("
            "SELECT order_id, line_no FROM shop.order_items "
            "ORDER BY md5(order_id::text || '-' || line_no::text || :salt) LIMIT :n)"
        ),
        {"salt": str(state.seed), "n": n},
    ).rowcount
    return [
        _expect(
            state, "negative_quantity", FindingCategory.COLUMN_VALUE, "shop.order_items",
            "quantity", "out_of_range", "1.5",
            f"{affected} order lines got a negative quantity (minimum was 1)",
            {"rows": affected},
        )
    ]  # fmt: skip


# --- time series (ETL faults, applied on the next simulated day) ------------------------


@_scenario("skipped_load", FindingCategory.TIME_SERIES, "The nightly reporting load doesn't run")
def skipped_load(conn: Connection, state: LabState) -> list[ExpectedAnomaly]:
    state.pending_faults["skip_reporting_load"] = True
    description = "The nightly ETL skipped its run; the table was not refreshed"
    return [
        _expect(
            state, "skipped_load", FindingCategory.TIME_SERIES, "reporting.daily_sales", None,
            "freshness", "1.5", description, {"missing_day": _effective(state).isoformat()},
        ),
        _expect(
            state, "skipped_load", FindingCategory.TIME_SERIES, "reporting.customer_summary",
            None, "freshness", "1.5", description, {},
        ),
    ]  # fmt: skip


@_scenario("half_load", FindingCategory.TIME_SERIES, "Only part of a day's orders arrive")
def half_load(conn: Connection, state: LabState) -> list[ExpectedAnomaly]:
    state.pending_faults["volume_factor"] = HALF_LOAD_VOLUME_FACTOR
    return [
        _expect(
            state, "half_load", FindingCategory.TIME_SERIES, "shop.orders", None,
            "volume_drop", "1.5",
            f"Only {HALF_LOAD_VOLUME_FACTOR:.0%} of the usual daily orders arrived",
            {"volume_factor": HALF_LOAD_VOLUME_FACTOR},
            related=(
                "shop.order_items", "shop.payments", "legacy.INV_HDR", "legacy.INV_LINE",
                "legacy.INV_LINE_TAX", "reporting.daily_sales",
            ),
        )
    ]  # fmt: skip


# --- business rules (detected by AI-proposed, human-approved rules in Stage 2) ----------


@_scenario("ship_before_order", FindingCategory.BUSINESS_RULE, "Parcels shipped before ordered")
def ship_before_order(conn: Connection, state: LabState) -> list[ExpectedAnomaly]:
    n = _sample_size(conn, "SELECT count(*) FROM shop.shipments", SHIP_BEFORE_ORDER_FRACTION)
    affected = conn.execute(
        text(
            "UPDATE shop.shipments s SET shipped_at = o.order_date - interval '2 days' "
            "FROM shop.orders o WHERE o.order_id = s.ord_id AND s.shipment_id IN ("
            "SELECT shipment_id FROM shop.shipments "
            "ORDER BY md5(shipment_id::text || :salt) LIMIT :n)"
        ),
        {"salt": str(state.seed), "n": n},
    ).rowcount
    return [
        _expect(
            state, "ship_before_order", FindingCategory.BUSINESS_RULE, "shop.shipments",
            "shipped_at", "rule:shipped_at>=order_date", "2",
            f"{affected} shipments have shipped_at two days before their order date",
            {"rows": affected}, related=("shop.orders",),
        )
    ]  # fmt: skip


@_scenario(
    "invoice_total_mismatch", FindingCategory.BUSINESS_RULE, "Invoice totals != sum of lines"
)
def invoice_total_mismatch(conn: Connection, state: LabState) -> list[ExpectedAnomaly]:
    n = _sample_size(conn, 'SELECT count(*) FROM legacy."INV_HDR"', INVOICE_MISMATCH_FRACTION)
    affected = conn.execute(
        text(
            'UPDATE legacy."INV_HDR" '
            'SET "TOTAL_AMT" = round("TOTAL_AMT" * CAST(:factor AS numeric), 2) '
            'WHERE "INV_NO" IN (SELECT "INV_NO" FROM legacy."INV_HDR" '
            'ORDER BY md5("INV_NO"::text || :salt) LIMIT :n)'
        ),
        {"factor": INVOICE_MISMATCH_FACTOR, "salt": str(state.seed), "n": n},
    ).rowcount
    return [
        _expect(
            state, "invoice_total_mismatch", FindingCategory.BUSINESS_RULE, "legacy.INV_HDR",
            "TOTAL_AMT", "rule:TOTAL_AMT=sum(INV_LINE.LINE_AMT)", "2",
            f"{affected} invoice totals inflated by 10% versus the sum of their lines",
            {"rows": affected, "factor": INVOICE_MISMATCH_FACTOR},
            related=("legacy.INV_LINE",),
        )
    ]  # fmt: skip
