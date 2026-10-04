"""Builds the lab company and simulates its business, one day at a time.

``build`` creates the catalog and customers, then replays ``size.history_days`` days of
activity through the *same* ``simulate_next_day`` used by ``tick``, so history and new
days are produced identically. Everything is seeded: the same seed and size always
give the same data (each day has its own RNG, derived from the seed and the date).

A simulated day:
1. a few new customers sign up (and most are mirrored into the legacy system)
2. orders arrive (fewer at weekends), with items and payments
3. fulfilment moves older orders: paid -> shipped -> delivered
4. the legacy billing batch invoices today's orders
5. the nightly ETL loads the reporting tables (unless a fault skips it)
6. the "application" runs its usual join queries (feeds query-log statistics)
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from faker import Faker
from sqlalchemy import Connection, text

from ai_data_engineer.lab.schema import assert_safe_lab_target, recreate_lab_schema
from ai_data_engineer.lab.sizes import DEFAULT_START_DATE, SIZES
from ai_data_engineer.lab.state import LabState, save_state

# --- behaviour of the simulated company (all tunable, none are "rules" about data) ----
WEEKDAY_FACTOR = (1.0, 1.05, 1.1, 1.1, 1.25, 0.65, 0.55)  # Monday .. Sunday
DAILY_NOISE = (0.9, 1.1)
NEW_CUSTOMERS_PER_DAY_RATIO = 0.01
CANCEL_RATE = 0.03
MIDDLE_NAME_RATE = 0.4
PHONE_RATE = 0.6
SECOND_ADDRESS_RATE = 0.2
LEGACY_MIRROR_RATE = 0.85
LEGACY_ONLY_RATE = 0.05
INACTIVE_PRODUCT_RATE = 0.05
MAX_ITEMS_PER_ORDER = 4
MAX_QUANTITY = 3
DAYS_TO_SHIP = 2
DAYS_TO_DELIVER = 3
INVOICE_PAID_AFTER_DAYS = 14
TAX_RATE = Decimal("0.10")
REPORTING_LOAD_HOUR = 2  # the nightly ETL finishes at 02:00 the next morning

CHANNELS = (("web", 55), ("app", 30), ("store", 10), ("phone", 5))
PAYMENT_METHODS = (("card", 70), ("paypal", 20), ("bank_transfer", 10))
COUNTRIES = (("US", 60), ("CA", 15), ("GB", 15), ("DE", 10))
CATEGORY_TREE = {
    "Electronics": ("Phones", "Laptops", "Audio"),
    "Home": ("Kitchen", "Furniture"),
    "Books": ("Fiction", "Non-fiction"),
    "Digital": ("E-books", "Software"),
}
DIGITAL_ROOT = "Digital"  # digital products have no weight (a normal NULL pattern)
PRICE_RANGE = {"Electronics": (49, 1500), "Home": (10, 600), "Books": (5, 60), "Digital": (3, 200)}
ADJECTIVES = ("Classic", "Smart", "Compact", "Premium", "Eco", "Pro", "Mini", "Ultra")
NOUNS = ("Speaker", "Lamp", "Chair", "Novel", "Phone", "Kettle", "Guide", "Suite", "Desk")

APP_QUERIES = (
    "SELECT o.order_id, c.email FROM shop.orders o JOIN shop.customers c ON o.cust_no = c.id "
    "WHERE o.order_date >= :since LIMIT 50",
    "SELECT p.amount FROM shop.payments p JOIN shop.orders o ON p.order_ref = o.order_id "
    "WHERE o.order_id = :order_id",
    "SELECT s.tracking_no FROM shop.shipments s JOIN shop.orders o ON s.ord_id = o.order_id "
    "WHERE o.cust_no = :customer_id",
    "SELECT i.quantity, p.sku FROM shop.order_items i JOIN shop.products p "
    "ON i.product_id = p.id WHERE i.order_id = :order_id",
    'SELECT h."INV_NO", l."LINE_AMT" FROM legacy."INV_HDR" h JOIN legacy."INV_LINE" l '
    'ON l."INV_NO" = h."INV_NO" WHERE h."CUSTID" = :custid',
)

_FAKER: Faker | None = None


@dataclass(frozen=True)
class _Order:
    order_id: int
    customer_code: str
    order_date: datetime
    total: Decimal
    items: tuple[tuple[str, int, Decimal], ...]  # (sku, quantity, unit_price)


def build(
    conn: Connection, *, seed: int, size: str, start_date: date = DEFAULT_START_DATE
) -> LabState:
    """(Re)create the lab database from scratch and simulate its history."""
    lab_size = SIZES[size]
    assert_safe_lab_target(conn)
    recreate_lab_schema(conn)

    state = LabState(
        seed=seed, size=size, start_date=start_date, current_day=start_date - timedelta(days=1)
    )
    rng = random.Random(f"{seed}:build")
    fake = _faker(rng)
    _create_catalog(conn, rng, lab_size.products, start_date)
    _create_customers(
        conn, state, rng, fake, lab_size.customers, start_date - timedelta(days=1), backfill=True
    )
    save_state(conn, state)
    for _ in range(lab_size.history_days):
        simulate_next_day(conn, state)
    return state


def simulate_next_day(conn: Connection, state: LabState) -> date:
    """Simulate one business day after ``state.current_day`` and persist the state."""
    day = state.current_day + timedelta(days=1)
    lab_size = SIZES[state.size]
    rng = random.Random(f"{state.seed}:{day.isoformat()}")
    fake = _faker(rng)
    faults, state.pending_faults = state.pending_faults, {}

    new_customers = round(lab_size.customers * NEW_CUSTOMERS_PER_DAY_RATIO * rng.uniform(0.5, 1.5))
    _create_customers(conn, state, rng, fake, new_customers, day)

    volume = float(faults.get("volume_factor", 1.0))
    expected = lab_size.orders_per_day * WEEKDAY_FACTOR[day.weekday()] * rng.uniform(*DAILY_NOISE)
    orders = _create_orders(conn, state, rng, day, max(1, round(expected * volume)))

    _advance_fulfilment(conn, day)
    _legacy_billing_batch(conn, state, day, orders)
    if not faults.get("skip_reporting_load"):
        _load_reporting(conn, day)
    _run_app_queries(conn, state, rng, day)

    state.current_day = day
    save_state(conn, state)
    return day


# --- steps ----------------------------------------------------------------------------


def _create_catalog(conn: Connection, rng: random.Random, n_products: int, start: date) -> None:
    categories: list[dict[str, Any]] = []
    leaves: list[tuple[int, str]] = []  # (category id, root name)
    for root, children in CATEGORY_TREE.items():
        root_id = len(categories) + 1
        categories.append({"id": root_id, "name": root, "parent_id": None})
        for child in children:
            child_id = len(categories) + 1
            categories.append({"id": child_id, "name": child, "parent_id": root_id})
            leaves.append((child_id, root))
    _insert(
        conn,
        "INSERT INTO shop.categories (id, name, parent_id) VALUES (:id, :name, :parent_id)",
        categories,
    )

    created_at = _at(start - timedelta(days=400), 9)
    products = []
    for product_id in range(1, n_products + 1):
        category_id, root = rng.choice(leaves)
        low, high = PRICE_RANGE[root]
        products.append(
            {
                "id": product_id,
                "sku": f"SKU-{product_id:05d}",
                "name": f"{rng.choice(ADJECTIVES)} {rng.choice(NOUNS)}",
                "category_id": category_id,
                "unit_price": _money(rng.uniform(low, high)),
                "weight_grams": None if root == DIGITAL_ROOT else rng.randint(50, 20_000),
                "is_active": rng.random() >= INACTIVE_PRODUCT_RATE,
                "created_at": created_at,
            }
        )
    _insert(
        conn,
        "INSERT INTO shop.products (id, sku, name, category_id, unit_price, weight_grams, "
        "is_active, created_at) VALUES (:id, :sku, :name, :category_id, :unit_price, "
        ":weight_grams, :is_active, :created_at)",
        products,
    )


def _create_customers(
    conn: Connection,
    state: LabState,
    rng: random.Random,
    fake: Faker,
    count: int,
    day: date,
    *,
    backfill: bool = False,
) -> None:
    """Sign up ``count`` customers. ``backfill`` spreads sign-up dates over the past year
    and also creates the legacy-only customers that exist before the app."""
    customers, addresses, legacy = [], [], []
    for _ in range(count):
        customer_id = state.take("customer")
        code = f"CU{customer_id:08d}"
        first, last = fake.first_name(), fake.last_name()
        signup_day = day - timedelta(days=rng.randint(0, 365)) if backfill else day
        created_at = _at(signup_day, rng.randint(6, 22))
        customers.append(
            {
                "id": customer_id,
                "code": code,
                "first": first,
                "middle": fake.first_name() if rng.random() < MIDDLE_NAME_RATE else None,
                "last": last,
                "email": f"{first}.{last}.{customer_id}@{fake.free_email_domain()}".lower(),
                "phone": fake.phone_number() if rng.random() < PHONE_RATE else None,
                "created_at": created_at,
            }
        )
        for n in range(2 if rng.random() < SECOND_ADDRESS_RATE else 1):
            addresses.append(
                {
                    "id": state.take("address"),
                    "customer_id": customer_id,
                    "line1": fake.street_address(),
                    "city": fake.city(),
                    "postal_code": fake.postcode(),
                    "country": _weighted(rng, COUNTRIES),
                    "is_default": n == 0,
                }
            )
        if rng.random() < LEGACY_MIRROR_RATE:
            legacy.append(
                {
                    "custid": f"C{customer_id:07d}",
                    "name": f"{first} {last}".upper()[:60],
                    "ext_ref": code,
                    "crt_dt": signup_day,
                }
            )
    if backfill:
        for _ in range(round(count * LEGACY_ONLY_RATE)):
            legacy_id = state.take("legacy_customer")
            legacy.append(
                {
                    "custid": f"C{legacy_id:07d}",
                    "name": fake.company().upper()[:60],
                    "ext_ref": None,
                    "crt_dt": day - timedelta(days=rng.randint(400, 3000)),
                }
            )

    _insert(
        conn,
        "INSERT INTO shop.customers (id, customer_code, first_name, middle_name, last_name, "
        "email, phone, created_at) VALUES (:id, :code, :first, :middle, :last, :email, "
        ":phone, :created_at)",
        customers,
    )
    _insert(
        conn,
        "INSERT INTO shop.addresses (id, customer_id, line1, city, postal_code, country, "
        "is_default) VALUES (:id, :customer_id, :line1, :city, :postal_code, :country, "
        ":is_default)",
        addresses,
    )
    _insert(
        conn,
        'INSERT INTO legacy."CUST_MASTER" ("CUSTID", "CUST_NM", "EXT_REF", "CRT_DT") '
        "VALUES (:custid, :name, :ext_ref, :crt_dt)",
        legacy,
    )


def _create_orders(
    conn: Connection, state: LabState, rng: random.Random, day: date, count: int
) -> list[_Order]:
    customers = conn.execute(text("SELECT id, customer_code FROM shop.customers ORDER BY id")).all()
    default_address = {
        row.customer_id: row.address_id
        for row in conn.execute(
            text(
                "SELECT customer_id, min(id) AS address_id FROM shop.addresses GROUP BY customer_id"
            )
        )
    }
    products = conn.execute(
        text("SELECT id, sku, unit_price FROM shop.products WHERE is_active ORDER BY id")
    ).all()

    orders, items, payments, created = [], [], [], []
    for _ in range(count):
        customer = rng.choice(customers)
        order_id = state.take("order")
        order_date = _at(day, rng.randint(6, 23), rng.randint(0, 59))
        picked = rng.sample(products, k=min(len(products), rng.randint(1, MAX_ITEMS_PER_ORDER)))
        lines = [(p.id, p.sku, rng.randint(1, MAX_QUANTITY), p.unit_price) for p in picked]
        total = sum((qty * price for _, _, qty, price in lines), Decimal("0"))
        cancelled = rng.random() < CANCEL_RATE
        orders.append(
            {
                "order_id": order_id,
                "cust_no": customer.id,
                "ship_addr": default_address.get(customer.id),
                "order_date": order_date,
                "status": "cancelled" if cancelled else "paid",
                "channel": _weighted(rng, CHANNELS),
                "total": total,
            }
        )
        for line_no, (product_id, _, qty, price) in enumerate(lines, start=1):
            items.append(
                {
                    "order_id": order_id,
                    "line_no": line_no,
                    "product_id": product_id,
                    "quantity": qty,
                    "unit_price": price,
                }
            )
        if cancelled:
            continue
        payments.append(
            {
                "id": state.take("payment"),
                "order_ref": order_id,
                "paid_at": order_date + timedelta(minutes=rng.randint(1, 30)),
                "amount": total,
                "method": _weighted(rng, PAYMENT_METHODS),
            }
        )
        created.append(
            _Order(
                order_id,
                customer.customer_code,
                order_date,
                total,
                tuple((sku, qty, price) for _, sku, qty, price in lines),
            )
        )

    _insert(
        conn,
        "INSERT INTO shop.orders (order_id, cust_no, ship_addr, order_date, status, channel, "
        "total_amount, currency) VALUES (:order_id, :cust_no, :ship_addr, :order_date, "
        ":status, :channel, :total, 'USD')",
        orders,
    )
    _insert(
        conn,
        "INSERT INTO shop.order_items (order_id, line_no, product_id, quantity, unit_price) "
        "VALUES (:order_id, :line_no, :product_id, :quantity, :unit_price)",
        items,
    )
    _insert(
        conn,
        "INSERT INTO shop.payments (id, order_ref, paid_at, amount, method, status) "
        "VALUES (:id, :order_ref, :paid_at, :amount, :method, 'captured')",
        payments,
    )
    return created


def _advance_fulfilment(conn: Connection, day: date) -> None:
    ship_cutoff = day - timedelta(days=DAYS_TO_SHIP)
    deliver_cutoff = day - timedelta(days=DAYS_TO_DELIVER)
    conn.execute(
        text(
            "INSERT INTO shop.shipments (ord_id, carrier, tracking_no, shipped_at) "
            "SELECT o.order_id, (ARRAY['UPS', 'FedEx', 'DHL', 'USPS'])[1 + (o.order_id % 4)::int], "
            "'TRK' || lpad(o.order_id::text, 10, '0'), "
            "o.order_date + make_interval(days => :ship_days) "
            "FROM shop.orders o WHERE o.status = 'paid' "
            "AND (o.order_date AT TIME ZONE 'UTC')::date <= :cutoff "
            "ORDER BY o.order_id"
        ),
        {"ship_days": DAYS_TO_SHIP, "cutoff": ship_cutoff},
    )
    conn.execute(
        text(
            "UPDATE shop.orders SET status = 'shipped' WHERE status = 'paid' "
            "AND (order_date AT TIME ZONE 'UTC')::date <= :cutoff"
        ),
        {"cutoff": ship_cutoff},
    )
    conn.execute(
        text(
            "UPDATE shop.shipments SET delivered_at = shipped_at + make_interval(days => :days) "
            "WHERE delivered_at IS NULL AND (shipped_at AT TIME ZONE 'UTC')::date <= :cutoff"
        ),
        {"days": DAYS_TO_DELIVER, "cutoff": deliver_cutoff},
    )
    conn.execute(
        text(
            "UPDATE shop.orders o SET status = 'delivered' FROM shop.shipments s "
            "WHERE s.ord_id = o.order_id AND o.status = 'shipped' AND s.delivered_at IS NOT NULL"
        )
    )


def _legacy_billing_batch(
    conn: Connection, state: LabState, day: date, orders: Sequence[_Order]
) -> None:
    mapping = {
        row.ext_ref: row.custid
        for row in conn.execute(
            text(
                'SELECT "EXT_REF" AS ext_ref, "CUSTID" AS custid FROM legacy."CUST_MASTER" '
                'WHERE "EXT_REF" IS NOT NULL'
            )
        )
    }
    headers, lines, taxes = [], [], []
    for order in orders:
        custid = mapping.get(order.customer_code)
        if custid is None:
            continue
        invoice_no = state.take("invoice")
        headers.append({"inv": invoice_no, "custid": custid, "day": day, "total": order.total})
        for line_no, (sku, qty, price) in enumerate(order.items, start=1):
            amount = qty * price
            lines.append(
                {"inv": invoice_no, "line": line_no, "sku": sku, "qty": qty, "amount": amount}
            )
            taxes.append({"inv": invoice_no, "line": line_no, "tax": _money(amount * TAX_RATE)})

    _insert(
        conn,
        'INSERT INTO legacy."INV_HDR" ("INV_NO", "CUSTID", "INV_DT", "TOTAL_AMT", "STAT_CD") '
        "VALUES (:inv, :custid, :day, :total, 'O')",
        headers,
    )
    _insert(
        conn,
        'INSERT INTO legacy."INV_LINE" ("INV_NO", "LINE_NO", "ITEM_CD", "QTY", "LINE_AMT") '
        "VALUES (:inv, :line, :sku, :qty, :amount)",
        lines,
    )
    _insert(
        conn,
        'INSERT INTO legacy."INV_LINE_TAX" ("INV_NO", "LINE_NO", "TAX_CD", "TAX_AMT") '
        "VALUES (:inv, :line, 'VAT', :tax)",
        taxes,
    )
    conn.execute(
        text(
            'UPDATE legacy."INV_HDR" SET "STAT_CD" = \'P\' '
            'WHERE "STAT_CD" = \'O\' AND "INV_DT" <= :cutoff'
        ),
        {"cutoff": day - timedelta(days=INVOICE_PAID_AFTER_DAYS)},
    )


def _load_reporting(conn: Connection, day: date) -> None:
    """The nightly ETL: append yesterday's sales, fully refresh the customer summary."""
    params = {"day": day, "loaded_at": _at(day + timedelta(days=1), REPORTING_LOAD_HOUR)}
    conn.execute(
        text(
            "INSERT INTO reporting.daily_sales (sales_date, orders_count, revenue, loaded_at) "
            "SELECT :day, count(*), coalesce(sum(total_amount), 0), :loaded_at "
            "FROM shop.orders WHERE status <> 'cancelled' "
            "AND (order_date AT TIME ZONE 'UTC')::date = :day "
            "ON CONFLICT (sales_date) DO UPDATE SET orders_count = EXCLUDED.orders_count, "
            "revenue = EXCLUDED.revenue, loaded_at = EXCLUDED.loaded_at"
        ),
        params,
    )
    conn.execute(text("TRUNCATE reporting.customer_summary"))
    conn.execute(
        text(
            "INSERT INTO reporting.customer_summary (customer_id, lifetime_orders, "
            "lifetime_revenue, last_order_date, refreshed_at) "
            "SELECT cust_no, count(*), sum(total_amount), "
            "max((order_date AT TIME ZONE 'UTC')::date), :loaded_at "
            "FROM shop.orders WHERE status <> 'cancelled' GROUP BY cust_no"
        ),
        params,
    )


def _run_app_queries(conn: Connection, state: LabState, rng: random.Random, day: date) -> None:
    """The 'application' joining tables the way real code would. Only some hidden
    relationships appear here, as in real life; discovery must find the rest otherwise."""
    last_order = state.next_ids["order"] - 1
    params = {
        "since": _at(day, 0),
        "order_id": rng.randint(1, max(1, last_order)),
        "customer_id": rng.randint(1, state.next_ids["customer"] - 1),
        "custid": f"C{rng.randint(1, state.next_ids['customer'] - 1):07d}",
    }
    for query in APP_QUERIES:
        for _ in range(rng.randint(3, 8)):
            conn.execute(text(query), params).all()


# --- helpers --------------------------------------------------------------------------


def _faker(rng: random.Random) -> Faker:
    global _FAKER  # one shared instance, re-seeded per use (creating Faker is slow)
    if _FAKER is None:
        _FAKER = Faker("en_US")
    _FAKER.seed_instance(rng.getrandbits(32))
    return _FAKER


def _insert(conn: Connection, sql: str, rows: list[dict[str, Any]]) -> None:
    if rows:
        conn.execute(text(sql), rows)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=UTC)


def _money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _weighted(rng: random.Random, options: tuple[tuple[str, int], ...]) -> str:
    values = [value for value, _ in options]
    weights = [weight for _, weight in options]
    return rng.choices(values, weights=weights)[0]
