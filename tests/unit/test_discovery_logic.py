"""Discovery logic that needs no database: naming, query-log parsing, scoring, selection,
candidate gates."""

import uuid

import pytest

from ai_data_engineer.discovery.candidates import Candidate, generate_candidates
from ai_data_engineer.discovery.catalog import Catalog, ColumnInfo, TableInfo
from ai_data_engineer.discovery.naming import name_score, tokens
from ai_data_engineer.discovery.querylog import join_counts, join_pairs
from ai_data_engineer.discovery.scoring import choose_best, score
from ai_data_engineer.discovery.settings import DiscoverySettings
from ai_data_engineer.graph.models import DataSource, SourceKind, TypeFamily
from ai_data_engineer.ingestion.base import QueryStat

SETTINGS = DiscoverySettings()


# --- naming ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("cust_no", ["customer", "number"]),
        ("CUSTID", ["customer", "id"]),
        ("orderRef", ["order", "reference"]),
        ("INV_LINE_TAX", ["invoice", "line", "tax"]),
        ("addresses", ["address"]),
        ("categories", ["category"]),
        ("status", ["status"]),
        ("EXT_REF", ["external", "reference"]),
    ],
)
def test_tokens(identifier: str, expected: list[str]) -> None:
    assert tokens(identifier) == expected


@pytest.mark.parametrize(
    ("child", "parent_table", "parent_column", "expected"),
    [
        ("cust_no", "customers", "id", 0.85),  # entity + key word, single-entity table
        ("cust_no", "customer_summary", "customer_id", 0.8),  # weaker: two-word table
        ("ship_addr", "addresses", "id", 0.65),  # entity, no key word
        ("CUSTID", "CUST_MASTER", "CUSTID", 1.0),  # identical name
        ("id", "customers", "id", 0.0),  # generic "id" is never a reference
        ("EXT_REF", "customers", "customer_code", 0.0),  # name says nothing
        ("orders_count", "orders", "order_id", 0.0),  # a measure, not a reference
        ("lifetime_orders", "orders", "order_id", 0.0),  # plural = a count of orders
        ("quantity", "categories", "id", 0.0),
    ],
)
def test_name_score(child: str, parent_table: str, parent_column: str, expected: float) -> None:
    assert name_score(child, parent_table, parent_column) == pytest.approx(expected)


# --- query log ------------------------------------------------------------------------------


def test_join_pairs_resolves_aliases_and_quoted_names() -> None:
    sql = (
        "SELECT o.order_id FROM shop.orders o JOIN shop.customers c ON o.cust_no = c.id "
        "WHERE o.order_date >= $1 LIMIT $2"
    )
    assert join_pairs(sql) == [(("shop", "orders", "cust_no"), ("shop", "customers", "id"))]

    legacy = (
        'SELECT h."INV_NO" FROM legacy."INV_HDR" h JOIN legacy."INV_LINE" l '
        'ON l."INV_NO" = h."INV_NO" WHERE h."CUSTID" = $1'
    )
    assert join_pairs(legacy) == [
        (("legacy", "INV_LINE", "INV_NO"), ("legacy", "INV_HDR", "INV_NO"))
    ]


def test_join_pairs_ignores_what_it_cannot_resolve() -> None:
    assert join_pairs("SELECT * FROM a JOIN b ON x = y") == []  # unqualified columns
    assert join_pairs("SELECT * FROM t WHERE t.a = t.b") == []  # same table
    assert join_pairs("this is not sql ((") == []
    schema_less = join_pairs("SELECT 1 FROM orders o JOIN customers c ON o.cust_no = c.id")
    assert schema_less == [(("", "orders", "cust_no"), ("", "customers", "id"))]


def test_join_counts_add_up_calls() -> None:
    sql = "SELECT 1 FROM s.a x JOIN s.b y ON x.k = y.id"
    counts = join_counts([QueryStat(sql, 10), QueryStat(sql + " WHERE x.z = $1", 5)])
    assert counts[frozenset({("s", "a", "k"), ("s", "b", "id")})] == 15


# --- scoring and selection -------------------------------------------------------------------


def _col(name: str, family: TypeFamily = TypeFamily.INTEGER, **stats: object) -> ColumnInfo:
    return ColumnInfo(uuid.uuid4(), name, family, family.value, **stats)  # type: ignore[arg-type]


def _table(name: str, *columns: ColumnInfo, pk: tuple[str, ...] = ("id",)) -> TableInfo:
    return TableInfo(
        asset_key=uuid.uuid4(),
        schema_name="shop",
        name=name,
        columns={c.name: c for c in columns},
        primary_key=pk,
        unique_keys=(),
        historical_keys=(),
        foreign_keys=(),
    )


CUSTOMERS = _table("customers", _col("id", distinct_count=100, row_count=100, null_count=0))
SUMMARY = _table(
    "customer_summary",
    _col("customer_id", distinct_count=90, row_count=90, null_count=0),
    pk=("customer_id",),
)
ORDERS = _table(
    "orders",
    _col("order_id", distinct_count=500, row_count=500, null_count=0),
    _col("cust_no", distinct_count=90, row_count=500, null_count=0),
    _col("quantity", distinct_count=3, row_count=500, null_count=0, min_repr="1", max_repr="3"),
    pk=("order_id",),
)


def _candidate(parent: TableInfo, parent_col: str, name: float, *, calls: int = 0) -> Candidate:
    c = Candidate(ORDERS, ("cust_no",), parent, (parent_col,), name, False, "primary_key")
    c.query_calls = calls
    return c


def test_confidence_formula() -> None:
    strong = _candidate(CUSTOMERS, "id", 0.85, calls=40)
    strong.inclusion, strong.values_checked = 1.0, 500
    assert score(strong, SETTINGS) == pytest.approx(0.5 + 0.25 * 0.85 + 0.25, abs=1e-3)

    with_orphans = _candidate(CUSTOMERS, "id", 0.85, calls=40)
    with_orphans.inclusion, with_orphans.values_checked = 0.98, 500
    assert score(with_orphans, SETTINGS) >= SETTINGS.orphan_check_min_confidence

    weak = _candidate(CUSTOMERS, "id", 0.85)
    weak.inclusion, weak.values_checked = 0.7, 500
    assert score(weak, SETTINGS) == 0.0
    assert any("below" in r for r in weak.reasons)


def test_choose_best_keeps_one_parent_per_child_column() -> None:
    to_customers = _candidate(CUSTOMERS, "id", 0.85, calls=40)
    to_customers.confidence = 0.96
    to_summary = _candidate(SUMMARY, "customer_id", 0.8)
    to_summary.confidence = 0.7
    assert choose_best([to_summary, to_customers]) == [to_customers]


def test_choose_best_drops_singles_covered_by_a_composite() -> None:
    tax = _table("inv_line_tax", _col("INV_NO"), _col("LINE_NO"), pk=())
    line = _table("inv_line", _col("INV_NO"), _col("LINE_NO"), pk=("INV_NO", "LINE_NO"))
    header = _table("inv_hdr", _col("INV_NO"), pk=("INV_NO",))
    composite = Candidate(
        tax, ("INV_NO", "LINE_NO"), line, ("INV_NO", "LINE_NO"), 1.0, False, "primary_key"
    )
    composite.confidence = 0.75
    single = Candidate(tax, ("INV_NO",), header, ("INV_NO",), 1.0, False, "primary_key")
    single.confidence = 0.8
    assert choose_best([composite, single]) == [composite]


def test_integer_candidates_without_a_name_need_query_log_evidence() -> None:
    catalog = Catalog(source=DataSource(name="t", kind=SourceKind.POSTGRES))
    for table in (CUSTOMERS, ORDERS):
        catalog.tables[table.ref] = table
    candidates = {
        (c.child_columns, c.parent.name): c for c in generate_candidates(catalog, set(), SETTINGS)
    }
    assert not candidates[(("cust_no",), "customers")].needs_query_log
    assert candidates[(("quantity",), "customers")].needs_query_log
    assert all(c.child_columns != ("id",) for c in candidates.values())  # generic ids skipped


def test_non_query_statements_are_skipped() -> None:
    assert join_pairs("show standard_conforming_strings") == []
    assert join_pairs("CREATE EXTENSION IF NOT EXISTS pg_stat_statements") == []


def test_own_primary_key_never_references_the_same_table() -> None:
    shipments = _table(
        "shipments",
        _col("shipment_id", distinct_count=50, row_count=50, null_count=0),
        _col("ord_id", distinct_count=50, row_count=50, null_count=0),  # measured unique
        pk=("shipment_id",),
    )
    catalog = Catalog(source=DataSource(name="t", kind=SourceKind.POSTGRES))
    catalog.tables[shipments.ref] = shipments
    pairs = {
        (c.child_columns, c.parent_columns)
        for c in generate_candidates(catalog, set(), SETTINGS)
    }
    assert (("shipment_id",), ("ord_id",)) not in pairs
