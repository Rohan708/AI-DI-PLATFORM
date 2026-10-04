"""Read queries: connected tables (recursive CTE), current structure, profile history."""

import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.graph.models import (
    AssetColumn,
    AssetKind,
    ColumnProfile,
    DataSource,
    IngestionRun,
    Relationship,
    RelationshipColumn,
    RelationshipKind,
    RelationshipStatus,
    Tenant,
    TypeFamily,
    relationship_signature,
)
from ai_data_engineer.graph.queries import (
    Direction,
    get_column_profile_history,
    get_connected_assets,
    get_current_structure,
)
from ai_data_engineer.graph.versioning import (
    AssetObservation,
    ColumnObservation,
    mark_missing_assets,
    record_asset,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class Graph:
    """Builds tables and relationships for one tenant/source."""

    def __init__(self, session: Session, run: IngestionRun) -> None:
        self.session = session
        self.run = run
        self.keys: dict[str, uuid.UUID] = {}

    def tables(self, *names: str) -> None:
        for name in names:
            result = record_asset(
                self.session,
                self.run,
                AssetObservation(
                    namespace="shop",
                    schema_name="public",
                    name=name,
                    kind=AssetKind.TABLE,
                    columns=(
                        ColumnObservation("id", "integer", TypeFamily.INTEGER, False, 1),
                        ColumnObservation("code", "text", TypeFamily.STRING, True, 2),
                    ),
                ),
                observed_at=T0,
            )
            self.keys[name] = result.asset_key

    def column(self, table: str, name: str) -> uuid.UUID:
        return self.session.scalars(
            select(AssetColumn.column_key).where(
                AssetColumn.asset_key == self.keys[table], AssetColumn.name == name
            )
        ).one()

    def link(
        self,
        child: str,
        parent: str,
        *,
        status: RelationshipStatus = RelationshipStatus.CONFIRMED,
        kind: RelationshipKind = RelationshipKind.DECLARED,
        columns: tuple[str, ...] = ("id",),
        closed: bool = False,
    ) -> Relationship:
        pairs = [(self.column(child, c), self.column(parent, c)) for c in columns]
        rel = Relationship(
            tenant_id=self.run.tenant_id,
            from_asset_key=self.keys[child],
            to_asset_key=self.keys[parent],
            kind=kind,
            status=status,
            confidence=0.8 if kind is RelationshipKind.INFERRED else None,
            signature=relationship_signature(pairs),
            valid_from=T0,
            valid_to=T0 + timedelta(days=1) if closed else None,
            columns=[
                RelationshipColumn(
                    tenant_id=self.run.tenant_id, ordinal=i, from_column_key=f, to_column_key=t
                )
                for i, (f, t) in enumerate(pairs)
            ],
        )
        self.session.add(rel)
        self.session.flush()
        return rel

    def connected(
        self,
        start: str,
        *,
        max_depth: int = 3,
        direction: Direction = "both",
        include_proposed: bool = False,
    ) -> dict[str, int]:
        rows = get_connected_assets(
            self.session,
            self.run.tenant_id,
            self.keys[start],
            max_depth=max_depth,
            direction=direction,
            include_proposed=include_proposed,
        )
        return {row.name: row.depth for row in rows}


@pytest.fixture
def graph(session: Session, run: IngestionRun) -> Graph:
    return Graph(session, run)


@pytest.fixture
def shop(graph: Graph) -> Graph:
    """customers <- orders <- order_items;  orders -> stores;  payments -> orders"""
    graph.tables("customers", "orders", "order_items", "stores", "payments")
    graph.link("orders", "customers")
    graph.link("order_items", "orders")
    graph.link("orders", "stores")
    graph.link("payments", "orders")
    return graph


# --- connectivity ---------------------------------------------------------------------------


def test_connected_both_directions(shop: Graph) -> None:
    assert shop.connected("customers") == {
        "orders": 1,
        "order_items": 2,
        "payments": 2,
        "stores": 2,
    }


def test_connected_parents_only(shop: Graph) -> None:
    assert shop.connected("orders", direction="parents") == {"customers": 1, "stores": 1}


def test_connected_children_only(shop: Graph) -> None:
    assert shop.connected("orders", direction="children") == {"order_items": 1, "payments": 1}


def test_depth_limit(shop: Graph) -> None:
    assert shop.connected("customers", max_depth=1) == {"orders": 1}


def test_diamond_reports_each_table_once_at_shortest_depth(graph: Graph) -> None:
    graph.tables("a", "b", "c", "d")
    graph.link("a", "b")
    graph.link("a", "c")
    graph.link("b", "d")
    graph.link("c", "d")
    assert graph.connected("a", direction="parents") == {"b": 1, "c": 1, "d": 2}


def test_cycle_terminates(graph: Graph) -> None:
    graph.tables("x", "y", "z")
    graph.link("x", "y")
    graph.link("y", "z")
    graph.link("z", "x")
    assert graph.connected("x", direction="parents", max_depth=10) == {"y": 1, "z": 2}
    assert graph.connected("x", max_depth=10) == {"y": 1, "z": 1}


def test_rejected_and_closed_relationships_are_not_followed(graph: Graph) -> None:
    graph.tables("orders", "customers", "legacy_customers")
    graph.link("orders", "customers", status=RelationshipStatus.REJECTED)
    graph.link("orders", "legacy_customers", closed=True)
    assert graph.connected("orders") == {}


def test_proposed_relationships_only_when_asked(graph: Graph) -> None:
    graph.tables("invoices", "customers")
    graph.link(
        "invoices",
        "customers",
        status=RelationshipStatus.PROPOSED,
        kind=RelationshipKind.INFERRED,
    )
    assert graph.connected("invoices") == {}
    assert graph.connected("invoices", include_proposed=True) == {"customers": 1}


def test_composite_relationship_keeps_column_order(graph: Graph, session: Session) -> None:
    graph.tables("order_lines", "orders")
    rel = graph.link("order_lines", "orders", columns=("id", "code"))
    session.expire(rel)
    assert [c.ordinal for c in rel.columns] == [0, 1]
    assert rel.columns[1].from_column_key == graph.column("order_lines", "code")
    assert graph.connected("order_lines") == {"orders": 1}


def test_other_tenant_sees_nothing(
    shop: Graph, make_tenant: Callable[[], Tenant], session: Session
) -> None:
    other_tenant = make_tenant()
    rows = get_connected_assets(session, other_tenant.id, shop.keys["customers"])
    assert rows == []


@pytest.mark.parametrize("depth", [0, 11])
def test_invalid_depth_is_rejected(shop: Graph, depth: int) -> None:
    with pytest.raises(ValueError, match="max_depth"):
        shop.connected("customers", max_depth=depth)


# --- structure ----------------------------------------------------------------------------


def test_current_structure_excludes_deleted_tables_and_columns(
    session: Session,
    source: DataSource,
    make_run: Callable[[DataSource], IngestionRun],
) -> None:
    run1, run2 = make_run(source), make_run(source)
    orders = AssetObservation(
        namespace="shop",
        schema_name="public",
        name="orders",
        kind=AssetKind.TABLE,
        primary_key=("id",),
        columns=(
            ColumnObservation("total", "numeric", TypeFamily.DECIMAL, True, 3),
            ColumnObservation("id", "integer", TypeFamily.INTEGER, False, 1),
            ColumnObservation("note", "text", TypeFamily.STRING, True, 2),
        ),
    )
    old_table = replace(orders, name="old_orders", primary_key=())
    record_asset(session, run1, orders, observed_at=T0)
    record_asset(session, run1, old_table, observed_at=T0)

    t1 = T0 + timedelta(days=1)
    without_note = replace(orders, columns=(orders.columns[0], orders.columns[1]))
    kept = record_asset(session, run2, without_note, observed_at=t1)
    mark_missing_assets(session, run2, [kept.asset_key], observed_at=t1)

    [current] = get_current_structure(session, source.id)
    assert current.name == "orders"
    assert current.primary_key == ("id",)
    assert [c.name for c in current.columns] == ["id", "total"]  # ordered by position


# --- profile history ----------------------------------------------------------------------


def test_profile_history_newest_first_with_limit(
    session: Session,
    source: DataSource,
    make_run: Callable[[DataSource], IngestionRun],
    graph: Graph,
) -> None:
    graph.tables("orders")
    column_key = graph.column("orders", "id")
    for day in range(5):
        run = make_run(source)
        session.add(
            ColumnProfile(
                tenant_id=source.tenant_id,
                column_key=column_key,
                ingestion_run_id=run.id,
                measured_at=T0 + timedelta(days=day),
                null_rate=day / 100,
            )
        )
    session.flush()

    history = get_column_profile_history(session, column_key, limit=3)

    assert [p.null_rate for p in history] == [0.04, 0.03, 0.02]
