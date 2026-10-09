"""The lab company's database: a modern shop app, a legacy billing system, and a
reporting area loaded by a nightly ETL.

It is messy on purpose:
- only some foreign keys are declared; the rest are hidden and often misnamed
  (``orders.cust_no`` -> ``customers.id``), see ``TRUE_RELATIONSHIPS``
- the legacy schema uses UPPERCASE names, CHAR types, no FKs, and a composite key
- some columns are legitimately sparse (``NORMAL_PATTERNS``), which a good detector
  must NOT flag
"""

from dataclasses import dataclass

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from ai_data_engineer.graph.models import FindingCategory

SHOP, LEGACY, REPORTING, LAB_ADMIN = "shop", "legacy", "reporting", "aide_lab"
# ``aide_lab`` holds the lab's own bookkeeping; scanners must exclude it.
LAB_SCHEMAS = (SHOP, LEGACY, REPORTING, LAB_ADMIN)

DDL = [
    f"CREATE SCHEMA {SHOP}",
    f"CREATE SCHEMA {LEGACY}",
    f"CREATE SCHEMA {REPORTING}",
    f"CREATE SCHEMA {LAB_ADMIN}",
    # --- shop: the modern application -------------------------------------------------
    """CREATE TABLE shop.categories (
        id integer PRIMARY KEY,
        name varchar(100) NOT NULL,
        parent_id integer REFERENCES shop.categories (id)
    )""",
    """CREATE TABLE shop.products (
        id integer PRIMARY KEY,
        sku varchar(20) NOT NULL UNIQUE,
        name varchar(200) NOT NULL,
        category_id integer NOT NULL REFERENCES shop.categories (id),
        unit_price numeric(10, 2) NOT NULL,
        weight_grams integer,
        is_active boolean NOT NULL,
        created_at timestamptz NOT NULL
    )""",
    """CREATE TABLE shop.customers (
        id integer PRIMARY KEY,
        customer_code varchar(12) NOT NULL UNIQUE,
        first_name varchar(100) NOT NULL,
        middle_name varchar(100),
        last_name varchar(100) NOT NULL,
        email varchar(255),
        phone varchar(40),
        created_at timestamptz NOT NULL
    )""",
    """CREATE TABLE shop.addresses (
        id integer PRIMARY KEY,
        customer_id integer NOT NULL REFERENCES shop.customers (id),
        line1 varchar(200) NOT NULL,
        city varchar(100) NOT NULL,
        postal_code varchar(12),
        country varchar(60) NOT NULL,
        is_default boolean NOT NULL
    )""",
    # cust_no and ship_addr are FKs in reality, never declared.
    """CREATE TABLE shop.orders (
        order_id bigint PRIMARY KEY,
        cust_no integer NOT NULL,
        ship_addr integer,
        order_date timestamptz NOT NULL,
        status varchar(20) NOT NULL,
        channel varchar(20) NOT NULL,
        total_amount numeric(12, 2) NOT NULL,
        currency char(3) NOT NULL
    )""",
    """CREATE TABLE shop.order_items (
        order_id bigint NOT NULL REFERENCES shop.orders (order_id),
        line_no integer NOT NULL,
        product_id integer NOT NULL,
        quantity integer NOT NULL,
        unit_price numeric(10, 2) NOT NULL,
        PRIMARY KEY (order_id, line_no)
    )""",
    """CREATE TABLE shop.payments (
        id bigint PRIMARY KEY,
        order_ref bigint NOT NULL,
        paid_at timestamptz NOT NULL,
        amount numeric(12, 2) NOT NULL,
        method varchar(20) NOT NULL,
        status varchar(20) NOT NULL
    )""",
    """CREATE TABLE shop.shipments (
        shipment_id bigserial PRIMARY KEY,
        ord_id bigint NOT NULL,
        carrier varchar(30) NOT NULL,
        tracking_no varchar(40) NOT NULL,
        shipped_at timestamptz NOT NULL,
        delivered_at timestamptz
    )""",
    # --- legacy: the old billing system ----------------------------------------------
    """CREATE TABLE legacy."CUST_MASTER" (
        "CUSTID" char(8) PRIMARY KEY,
        "CUST_NM" char(60) NOT NULL,
        "EXT_REF" varchar(12),
        "CRT_DT" date
    )""",
    """CREATE TABLE legacy."INV_HDR" (
        "INV_NO" integer PRIMARY KEY,
        "CUSTID" char(8) NOT NULL,
        "INV_DT" date NOT NULL,
        "TOTAL_AMT" numeric(12, 2) NOT NULL,
        "STAT_CD" char(1) NOT NULL
    )""",
    """CREATE TABLE legacy."INV_LINE" (
        "INV_NO" integer NOT NULL,
        "LINE_NO" smallint NOT NULL,
        "ITEM_CD" varchar(20) NOT NULL,
        "QTY" integer NOT NULL,
        "LINE_AMT" numeric(12, 2) NOT NULL,
        CONSTRAINT "INV_LINE_PK" PRIMARY KEY ("INV_NO", "LINE_NO")
    )""",
    """CREATE TABLE legacy."INV_LINE_TAX" (
        "INV_NO" integer NOT NULL,
        "LINE_NO" smallint NOT NULL,
        "TAX_CD" char(4) NOT NULL,
        "TAX_AMT" numeric(12, 2) NOT NULL
    )""",
    # --- reporting: loaded by the nightly ETL ----------------------------------------
    """CREATE TABLE reporting.daily_sales (
        sales_date date PRIMARY KEY,
        orders_count integer NOT NULL,
        revenue numeric(14, 2) NOT NULL,
        loaded_at timestamptz NOT NULL
    )""",
    """CREATE TABLE reporting.customer_summary (
        customer_id integer PRIMARY KEY,
        lifetime_orders integer NOT NULL,
        lifetime_revenue numeric(14, 2) NOT NULL,
        last_order_date date NOT NULL,
        refreshed_at timestamptz NOT NULL
    )""",
    # --- lab bookkeeping (not part of the "company") ----------------------------------
    "CREATE TABLE aide_lab.state (key text PRIMARY KEY, value jsonb NOT NULL)",
]


@dataclass(frozen=True)
class ExpectedRelationship:
    """A true link between tables. ``declared`` = a real FK constraint exists."""

    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]
    declared: bool
    note: str = ""


def _rel(
    src: str, cols: str, dst: str, dst_cols: str, *, declared: bool, note: str = ""
) -> ExpectedRelationship:
    return ExpectedRelationship(
        src, tuple(cols.split(",")), dst, tuple(dst_cols.split(",")), declared, note
    )


TRUE_RELATIONSHIPS: tuple[ExpectedRelationship, ...] = (
    _rel("shop.categories", "parent_id", "shop.categories", "id", declared=True),
    _rel("shop.products", "category_id", "shop.categories", "id", declared=True),
    _rel("shop.addresses", "customer_id", "shop.customers", "id", declared=True),
    _rel("shop.order_items", "order_id", "shop.orders", "order_id", declared=True),
    _rel("shop.orders", "cust_no", "shop.customers", "id", declared=False, note="misnamed"),
    _rel("shop.orders", "ship_addr", "shop.addresses", "id", declared=False, note="misnamed"),
    _rel("shop.order_items", "product_id", "shop.products", "id", declared=False),
    _rel("shop.payments", "order_ref", "shop.orders", "order_id", declared=False),
    _rel("shop.shipments", "ord_id", "shop.orders", "order_id", declared=False),
    _rel(
        "legacy.CUST_MASTER",
        "EXT_REF",
        "shop.customers",
        "customer_code",
        declared=False,
        note="cross-schema, links legacy to the app by business code; NULL for legacy-only",
    ),
    _rel("legacy.INV_HDR", "CUSTID", "legacy.CUST_MASTER", "CUSTID", declared=False),
    _rel("legacy.INV_LINE", "INV_NO", "legacy.INV_HDR", "INV_NO", declared=False),
    _rel(
        "legacy.INV_LINE",
        "ITEM_CD",
        "shop.products",
        "sku",
        declared=False,
        note="cross-schema, by business code",
    ),
    _rel(
        "legacy.INV_LINE_TAX",
        "INV_NO,LINE_NO",
        "legacy.INV_LINE",
        "INV_NO,LINE_NO",
        declared=False,
        note="composite key",
    ),
    _rel("reporting.customer_summary", "customer_id", "shop.customers", "id", declared=False),
)

@dataclass(frozen=True)
class ExpectedRule:
    """A business rule that holds in clean ShopCo data but is declared nowhere: what we
    hope the AI proposes (Stage 2). ``other`` is the compared column (same row), a
    ``schema.table.column`` in a parent table, or the summed child column; None for a
    comparison with a constant. The operator isn't scored."""

    kind: str
    table: str
    column: str
    other: str | None
    note: str


HIDDEN_RULES: tuple[ExpectedRule, ...] = (
    ExpectedRule("compare_columns", "shop.shipments", "shipped_at", "shop.orders.order_date",
                 "a parcel ships after it was ordered (scenario ship_before_order)"),
    ExpectedRule("compare_columns", "shop.shipments", "delivered_at", "shipped_at",
                 "delivered after shipped"),
    ExpectedRule("compare_columns", "shop.payments", "paid_at", "shop.orders.order_date",
                 "paid after ordering"),
    ExpectedRule("compare_columns", "shop.payments", "amount", "shop.orders.total_amount",
                 "one payment of the full order total"),
    ExpectedRule("sum_matches", "legacy.INV_HDR", "TOTAL_AMT", "legacy.INV_LINE.LINE_AMT",
                 "invoice total = sum of its lines (scenario invoice_total_mismatch)"),
    ExpectedRule("compare_constant", "shop.order_items", "quantity", None, "quantity > 0"),
    ExpectedRule("compare_constant", "shop.order_items", "unit_price", None, "price >= 0"),
    ExpectedRule("compare_constant", "shop.products", "unit_price", None, "price > 0"),
    ExpectedRule("compare_constant", "shop.orders", "total_amount", None, "total >= 0"),
    ExpectedRule("compare_constant", "shop.payments", "amount", None, "amount > 0"),
    ExpectedRule("compare_constant", "legacy.INV_LINE", "QTY", None, "quantity > 0"),
    ExpectedRule("compare_constant", "legacy.INV_LINE", "LINE_AMT", None, "amount >= 0"),
    ExpectedRule("compare_constant", "legacy.INV_LINE_TAX", "TAX_AMT", None, "tax >= 0"),
)  # fmt: skip


# Legitimate patterns that look odd but are NOT anomalies (false-alarm traps).
NORMAL_PATTERNS: tuple[str, ...] = (
    "shop.customers.middle_name is ~60% NULL (most people have none)",
    "shop.customers.phone is ~40% NULL (optional field)",
    "shop.products.weight_grams is NULL for every Digital product",
    "shop.shipments.delivered_at is NULL while a parcel is in transit",
    "shop.orders.ship_addr is NULL for customers without an address",
    "legacy.CUST_MASTER.EXT_REF is NULL for legacy-only customers (~5%)",
    "legacy CHAR columns are space-padded (CUST_NM, CUSTID, STAT_CD)",
    "order volume is ~40% lower on weekends (seasonality)",
    "shop.orders.status mix shifts daily as orders move placed -> shipped -> delivered",
)


@dataclass(frozen=True)
class BaselineIssue:
    """A real problem ShopCo has from day one (not planted). Finding it is correct; the
    scorer reports these separately instead of counting them as false alarms."""

    category: FindingCategory
    table: str
    column: str | None
    check_hint: str
    description: str


# Relationship child columns that happen to be indexed (the leading part of a key).
_INDEXED_CHILD_COLUMNS = {
    ("shop.order_items", ("order_id",)),
    ("reporting.customer_summary", ("customer_id",)),
    ("legacy.INV_LINE", ("INV_NO",)),
}

BASELINE_ISSUES: tuple[BaselineIssue, ...] = (
    BaselineIssue(
        FindingCategory.STRUCTURAL,
        "legacy.INV_LINE_TAX",
        None,
        "missing_primary_key",
        "legacy tax lines have never had a primary key",
    ),
    *(
        BaselineIssue(
            FindingCategory.STRUCTURAL,
            r.from_table,
            r.from_columns[0] if len(r.from_columns) == 1 else None,
            "unindexed_foreign_key",
            f"{r.from_table}({', '.join(r.from_columns)}) references {r.to_table} without an index",
        )
        for r in TRUE_RELATIONSHIPS
        if (r.from_table, r.from_columns) not in _INDEXED_CHILD_COLUMNS
    ),
)


class LabSafetyError(RuntimeError):
    """Raised when the target database doesn't look like a lab database."""


def assert_safe_lab_target(conn: Connection) -> None:
    """Refuse to (re)build unless the database contains only lab schemas and an empty
    ``public`` schema, so the lab can never wipe a real or metadata-store database."""
    schemas = set(
        conn.scalars(
            text(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT LIKE 'pg\\_%' AND schema_name <> 'information_schema'"
            )
        )
    )
    unexpected = schemas - {"public", *LAB_SCHEMAS}
    # Tables/views in public, ignoring objects owned by extensions: the lab itself
    # installs pg_stat_statements, whose views live in public.
    public_tables = conn.scalar(
        text(
            "SELECT count(*) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
            "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass "
            "AND d.objid = c.oid AND d.deptype = 'e')"
        )
    )
    if unexpected or public_tables:
        raise LabSafetyError(
            "refusing to build the lab here: the database has non-lab content "
            f"(schemas {sorted(unexpected)}, {public_tables} tables in public). "
            "Point AIDE_LAB_DATABASE_URL at the dedicated lab database."
        )


def recreate_lab_schema(conn: Connection) -> None:
    conn.execute(text(f"DROP SCHEMA IF EXISTS {', '.join(LAB_SCHEMAS)} CASCADE"))
    for statement in DDL:
        conn.execute(text(statement))
    # Query statistics feed relationship discovery (Stage 1.4). Creating the extension
    # needs the server preloading it (docker-compose does); skip quietly otherwise.
    savepoint = conn.begin_nested()
    try:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_stat_statements"))
        savepoint.commit()
    except DBAPIError:
        savepoint.rollback()
