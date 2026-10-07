"""The Postgres adapter against the real lab database: structure, measurements, safety,
and full scans into the metadata store."""

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import Any, Self

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from ai_data_engineer.db import create_db_engine
from ai_data_engineer.graph.models import (
    AssetColumn,
    AssetColumnVersion,
    AssetProfile,
    AssetVersion,
    ColumnProfile,
    DataSource,
    IngestionRun,
    RunStatus,
    SourceKind,
    TypeFamily,
)
from ai_data_engineer.graph.queries import get_current_structure
from ai_data_engineer.ingestion.base import (
    DiscoveredTable,
    InclusionResult,
    KeyRef,
    OrphanResult,
    QueryStat,
    TableMeasurement,
)
from ai_data_engineer.ingestion.postgres import PostgresAdapter
from ai_data_engineer.ingestion.scan import scan_source
from ai_data_engineer.ingestion.settings import ScanSettings
from ai_data_engineer.ingestion.sources import add_source, get_source
from ai_data_engineer.lab.runner import build_lab, inject, lab_scan_time, run_plan
from ai_data_engineer.lab.schema import TRUE_RELATIONSHIPS

SETTINGS = ScanSettings(exclude_schemas=("aide_lab",))
LAB_TABLES = {
    "shop.categories", "shop.products", "shop.customers", "shop.addresses", "shop.orders",
    "shop.order_items", "shop.payments", "shop.shipments", "legacy.CUST_MASTER",
    "legacy.INV_HDR", "legacy.INV_LINE", "legacy.INV_LINE_TAX", "reporting.daily_sales",
    "reporting.customer_summary",
}  # fmt: skip
T1 = datetime(2026, 3, 1, 3, tzinfo=UTC)
T2 = T1 + timedelta(days=1)


def _build_lab(make_database: Callable[[], str], seed: int = 11) -> str:
    url = make_database()
    engine = create_db_engine(url)
    build_lab(engine, seed=seed, size="tiny")
    engine.dispose()
    return url


def _sql(url: str, sql: str) -> Any:
    engine = create_db_engine(url)
    try:
        with engine.connect() as conn:
            return conn.scalar(text(sql))
    finally:
        engine.dispose()


def _count(session: Session, model: type[Any], *where: Any) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*where)) or 0


@pytest.fixture(scope="module")
def lab_url(make_database: Callable[[], str]) -> str:
    return _build_lab(make_database)


@pytest.fixture
def adapter(lab_url: str) -> Iterator[PostgresAdapter]:
    with PostgresAdapter(lab_url, SETTINGS) as pg:
        yield pg


@pytest.fixture
def tables(adapter: PostgresAdapter) -> dict[str, DiscoveredTable]:
    return {t.ref: t for t in adapter.introspect()}


# --- structure ------------------------------------------------------------------------------


def test_finds_every_lab_table_and_skips_excluded_schemas(
    tables: dict[str, DiscoveredTable],
) -> None:
    assert set(tables) == LAB_TABLES


def test_structure_details(tables: dict[str, DiscoveredTable]) -> None:
    def column(ref: str, name: str) -> Any:
        return next(c for c in tables[ref].observation.columns if c.name == name)

    assert tables["shop.order_items"].observation.primary_key == ("order_id", "line_no")
    assert tables["legacy.INV_LINE"].observation.primary_key == ("INV_NO", "LINE_NO")
    assert tables["legacy.INV_LINE_TAX"].observation.primary_key == ()  # legacy: no PK

    custid = column("legacy.CUST_MASTER", "CUSTID")
    assert (custid.native_type, custid.type_family) == ("character(8)", TypeFamily.STRING)
    total = column("shop.orders", "total_amount")
    assert (total.native_type, total.type_family) == ("numeric(12,2)", TypeFamily.DECIMAL)
    assert column("shop.orders", "order_date").type_family is TypeFamily.TIMESTAMP
    assert column("shop.products", "is_active").type_family is TypeFamily.BOOLEAN
    assert not column("shop.orders", "cust_no").is_nullable

    uniques = tables["shop.products"].observation.unique_constraints
    assert [u["columns"] for u in uniques] == [["sku"]]
    assert any(ix["primary"] for ix in tables["shop.orders"].observation.indexes)


def test_declared_foreign_keys_match_answer_key(tables: dict[str, DiscoveredTable]) -> None:
    found = {
        (ref, tuple(fk["columns"]), fk["ref_table"], tuple(fk["ref_columns"]))
        for ref, table in tables.items()
        for fk in table.observation.foreign_keys
    }
    expected = {
        (r.from_table, r.from_columns, r.to_table, r.to_columns)
        for r in TRUE_RELATIONSHIPS
        if r.declared
    }
    assert found == expected


# --- measurements ---------------------------------------------------------------------------


def test_measurements_match_direct_sql(
    adapter: PostgresAdapter, tables: dict[str, DiscoveredTable], lab_url: str
) -> None:
    customers = adapter.profile(tables["shop.customers"])
    by_name = {c.name: c for c in customers.columns}
    assert customers.sample_fraction == 1.0
    assert not customers.row_count_is_estimate
    assert customers.row_count == _sql(lab_url, "SELECT count(*) FROM shop.customers")
    assert by_name["middle_name"].null_count == _sql(
        lab_url, "SELECT count(*) FROM shop.customers WHERE middle_name IS NULL"
    )
    assert by_name["email"].distinct_count == _sql(
        lab_url, "SELECT count(DISTINCT email) FROM shop.customers"
    )
    assert by_name["id"].min_repr == "1"

    products = {c.name: c for c in adapter.profile(tables["shop.products"]).columns}
    assert products["weight_grams"].null_count == _sql(
        lab_url, "SELECT count(*) FROM shop.products WHERE weight_grams IS NULL"
    )
    assert products["unit_price"].mean == pytest.approx(
        float(_sql(lab_url, "SELECT avg(unit_price) FROM shop.products"))
    )


def test_top_values_for_low_cardinality_columns(
    adapter: PostgresAdapter, tables: dict[str, DiscoveredTable], lab_url: str
) -> None:
    country = next(
        c for c in adapter.profile(tables["shop.addresses"]).columns if c.name == "country"
    )
    assert country.top_values is not None
    assert {v["value"] for v in country.top_values} <= {"US", "CA", "GB", "DE"}
    assert sum(v["count"] for v in country.top_values) == _sql(
        lab_url, "SELECT count(*) FROM shop.addresses"
    )


def test_no_value_samples_setting_is_respected(
    lab_url: str, tables: dict[str, DiscoveredTable]
) -> None:
    with PostgresAdapter(lab_url, ScanSettings(allow_value_samples=False)) as private:
        columns = {c.name: c for c in private.profile(tables["shop.addresses"]).columns}
    assert columns["country"].top_values is None
    assert columns["city"].min_repr is None
    assert columns["id"].min_repr == "1"


def test_large_tables_are_sampled_and_flagged(
    lab_url: str, tables: dict[str, DiscoveredTable]
) -> None:
    with PostgresAdapter(lab_url, ScanSettings(sample_row_threshold=5)) as sampler:
        measurement = sampler.profile(tables["shop.orders"])
    assert measurement.sample_fraction < 1.0
    assert measurement.row_count_is_estimate
    assert all(c.distinct_is_approx for c in measurement.columns)


# --- safety ---------------------------------------------------------------------------------


def test_connection_is_read_only_with_time_limits(adapter: PostgresAdapter) -> None:
    with adapter.read_only() as conn:
        assert conn.scalar(text("SHOW transaction_read_only")) == "on"
        assert conn.scalar(text("SHOW statement_timeout")) == "30s"
        assert conn.scalar(text("SHOW lock_timeout")) == "2s"
        assert conn.scalar(text("SELECT current_setting('application_name')")) == "aide"
    with pytest.raises(DBAPIError, match="read-only transaction"), adapter.read_only() as conn:
        conn.execute(text("CREATE TABLE shop.should_fail (id int)"))


# --- scanning into the metadata store -------------------------------------------------------


def _factory(url: str, settings: ScanSettings = SETTINGS) -> Callable[[DataSource], Any]:
    return lambda _source: PostgresAdapter(url, settings)


def test_scan_records_structure_and_measurements(
    session: Session, source: DataSource, lab_url: str
) -> None:
    result = scan_source(session, source, observed_at=T1, adapter_factory=_factory(lab_url))

    assert result.status is RunStatus.SUCCEEDED
    assert result.tables_seen == result.tables_profiled == len(LAB_TABLES)
    assert len(result.tables_created) == len(LAB_TABLES)
    structure = get_current_structure(session, source.id)
    assert {f"{a.schema_name}.{a.name}" for a in structure} == LAB_TABLES
    assert _count(session, AssetProfile) == len(LAB_TABLES)
    assert _count(session, ColumnProfile) == sum(len(a.columns) for a in structure)
    run = session.get_one(IngestionRun, result.run_id)
    assert (run.started_at, run.status) == (T1, RunStatus.SUCCEEDED)


def test_rescan_without_changes_adds_measurements_but_no_versions(
    session: Session, source: DataSource, lab_url: str
) -> None:
    scan_source(session, source, observed_at=T1, adapter_factory=_factory(lab_url))
    versions = _count(session, AssetVersion), _count(session, AssetColumnVersion)

    second = scan_source(session, source, observed_at=T2, adapter_factory=_factory(lab_url))

    assert second.tables_created == second.tables_changed == []
    assert (_count(session, AssetVersion), _count(session, AssetColumnVersion)) == versions
    assert _count(session, AssetProfile) == 2 * len(LAB_TABLES)


def test_structural_changes_become_new_versions(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url = _build_lab(make_database, seed=12)
    scan_source(session, source, observed_at=T1, adapter_factory=_factory(url))
    lab: Engine = create_db_engine(url)
    inject(lab, ["drop_column", "widen_column_type", "drop_primary_key"])
    lab.dispose()

    result = scan_source(session, source, observed_at=T2, adapter_factory=_factory(url))

    assert set(result.tables_changed) == {"shop.products", "shop.orders", "legacy.INV_LINE"}
    weight = session.scalars(select(AssetColumn).where(AssetColumn.name == "weight_grams")).one()
    assert weight.deleted_at == T2
    channel_types = session.scalars(
        select(AssetColumnVersion.native_type)
        .join(AssetColumn, AssetColumn.column_key == AssetColumnVersion.column_key)
        .where(AssetColumn.name == "channel")
        .order_by(AssetColumnVersion.valid_from)
    ).all()
    assert channel_types == ["character varying(20)", "character varying(50)"]


class _FailOn:
    """Wraps a real adapter and makes profiling one table fail."""

    def __init__(self, inner: PostgresAdapter, ref: str) -> None:
        self.inner, self.ref = inner, ref

    def __enter__(self) -> Self:
        self.inner.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.inner.__exit__(exc_type, exc, tb)

    def introspect(self) -> list[DiscoveredTable]:
        return self.inner.introspect()

    def read_query_log(self, limit: int) -> list[QueryStat]:
        return self.inner.read_query_log(limit)

    def value_inclusion(self, child: KeyRef, parent: KeyRef, max_rows: int) -> InclusionResult:
        return self.inner.value_inclusion(child, parent, max_rows)

    def count_orphans(
        self, child: KeyRef, parent: KeyRef, row_ids: tuple[str, ...], sample_size: int
    ) -> OrphanResult:
        return self.inner.count_orphans(child, parent, row_ids, sample_size)

    def profile(self, table: DiscoveredTable) -> TableMeasurement:
        if table.ref == self.ref:
            raise OperationalError(
                "SELECT ...", {}, Exception("canceling statement due to timeout")
            )
        return self.inner.profile(table)


def test_one_failing_table_makes_the_run_partial(
    session: Session, source: DataSource, lab_url: str
) -> None:
    result = scan_source(
        session,
        source,
        observed_at=T1,
        adapter_factory=lambda _s: _FailOn(PostgresAdapter(lab_url, SETTINGS), "shop.orders"),
    )
    assert result.status is RunStatus.PARTIAL
    assert list(result.errors) == ["shop.orders"]
    assert result.tables_profiled == len(LAB_TABLES) - 1
    assert "timeout" in (session.get_one(IngestionRun, result.run_id).error_message or "")


def test_unreachable_database_fails_the_run(session: Session, source: DataSource) -> None:
    dead = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none"
    result = scan_source(session, source, observed_at=T1, adapter_factory=_factory(dead))
    assert result.status is RunStatus.FAILED
    assert session.get_one(IngestionRun, result.run_id).error_message


def test_registered_source_scans_through_its_connection_ref(
    session: Session, lab_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIDE_TEST_LAB_URL", lab_url)
    add_source(
        session,
        name="shopco",
        kind=SourceKind.POSTGRES,
        connection_ref="AIDE_TEST_LAB_URL",
        settings={"exclude_schemas": ["aide_lab"]},
    )
    source = get_source(session, "shopco")
    assert source.settings == {"exclude_schemas": ["aide_lab"]}  # only non-defaults stored

    result = scan_source(session, source, observed_at=T1)

    assert result.status is RunStatus.SUCCEEDED
    assert result.tables_seen == len(LAB_TABLES)


def test_add_source_rejects_unknown_settings(session: Session) -> None:
    with pytest.raises(ValidationError):
        add_source(
            session,
            name="typo",
            kind=SourceKind.POSTGRES,
            connection_ref="X",
            settings={"exclude_schema": ["aide_lab"]},
        )


def test_lab_plan_with_a_scan_after_every_day(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url = make_database()
    lab = create_db_engine(url)
    scanned: list[date] = []

    def nightly_scan(day: date) -> None:
        scan_source(session, source, observed_at=lab_scan_time(day), adapter_factory=_factory(url))
        scanned.append(day)

    run_plan(lab, "quick", seed=13, size="tiny", on_day_end=nightly_scan)
    lab.dispose()

    runs = session.scalars(
        select(IngestionRun)
        .where(IngestionRun.data_source_id == source.id)
        .order_by(IngestionRun.started_at)
    ).all()
    assert [r.started_at for r in runs] == [lab_scan_time(day) for day in scanned]
    assert all(r.status is RunStatus.SUCCEEDED for r in runs)
    assert runs[-1].stats["tables_changed"]  # the planted structural changes were seen
