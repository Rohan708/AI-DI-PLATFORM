"""History is recorded correctly: identities stay stable, versions open/close on change."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_data_engineer.graph.models import (
    Asset,
    AssetColumn,
    AssetColumnVersion,
    AssetKind,
    AssetVersion,
    DataSource,
    IngestionRun,
    Tenant,
    TypeFamily,
)
from ai_data_engineer.graph.versioning import (
    AssetObservation,
    ColumnObservation,
    mark_missing_assets,
    record_asset,
)

T1 = datetime(2026, 1, 1, tzinfo=UTC)
T2 = T1 + timedelta(days=1)
T3 = T2 + timedelta(days=1)

ID = ColumnObservation("id", "integer", TypeFamily.INTEGER, False, 1)
AMOUNT = ColumnObservation("amount", "numeric(10,2)", TypeFamily.DECIMAL, True, 2)
ORDERS = AssetObservation(
    namespace="shop",
    schema_name="public",
    name="orders",
    kind=AssetKind.TABLE,
    columns=(ID, AMOUNT),
    primary_key=("id",),
    indexes=({"name": "ix_orders_amount", "columns": ["amount"], "unique": False},),
)


def _count(session: Session, model: type[Any]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _column(session: Session, name: str) -> AssetColumn:
    return session.scalars(select(AssetColumn).where(AssetColumn.name == name)).one()


def _column_versions(session: Session, name: str) -> list[AssetColumnVersion]:
    column = _column(session, name)
    return list(
        session.scalars(
            select(AssetColumnVersion)
            .where(AssetColumnVersion.column_key == column.column_key)
            .order_by(AssetColumnVersion.valid_from)
        )
    )


@pytest.fixture
def runs(
    make_run: Callable[[DataSource], IngestionRun], source: DataSource
) -> tuple[IngestionRun, IngestionRun, IngestionRun]:
    return make_run(source), make_run(source), make_run(source)


def test_first_observation_creates_identity_and_versions(
    session: Session, runs: tuple[IngestionRun, ...]
) -> None:
    result = record_asset(session, runs[0], ORDERS, observed_at=T1)

    assert result.asset_created
    assert result.changed
    assert _count(session, Asset) == 1
    assert _count(session, AssetVersion) == 1
    assert _count(session, AssetColumn) == 2
    assert _count(session, AssetColumnVersion) == 2
    version = session.scalars(select(AssetVersion)).one()
    assert version.valid_to is None
    assert version.primary_key == ["id"]


def test_same_observation_again_creates_no_new_versions(
    session: Session, runs: tuple[IngestionRun, ...]
) -> None:
    first = record_asset(session, runs[0], ORDERS, observed_at=T1)
    # Same content, but tuples/lists and key order differ: must still count as "unchanged".
    reshuffled = replace(
        ORDERS, indexes=({"unique": False, "columns": ("amount",), "name": "ix_orders_amount"},)
    )
    second = record_asset(session, runs[1], reshuffled, observed_at=T2)

    assert not second.changed
    assert second.asset_key == first.asset_key
    assert _count(session, AssetVersion) == 1
    assert _count(session, AssetColumnVersion) == 2
    asset = session.get_one(Asset, first.asset_key)
    assert asset.first_seen_at == T1
    assert asset.last_seen_at == T2


def test_type_change_closes_old_version_and_opens_new(
    session: Session, runs: tuple[IngestionRun, ...]
) -> None:
    record_asset(session, runs[0], ORDERS, observed_at=T1)
    retyped = replace(
        ORDERS, columns=(ID, replace(AMOUNT, native_type="text", type_family=TypeFamily.STRING))
    )
    result = record_asset(session, runs[1], retyped, observed_at=T2)

    assert result.columns_changed == ["amount"]
    old, new = _column_versions(session, "amount")
    assert (old.type_family, old.valid_to) == (TypeFamily.DECIMAL, T2)
    assert (new.type_family, new.valid_from, new.valid_to) == (TypeFamily.STRING, T2, None)
    assert new.ingestion_run_id == runs[1].id
    assert len(_column_versions(session, "id")) == 1  # untouched column keeps one version


def test_dropped_then_readded_column_keeps_its_identity(
    session: Session, runs: tuple[IngestionRun, ...]
) -> None:
    record_asset(session, runs[0], ORDERS, observed_at=T1)
    key_before = _column(session, "amount").column_key

    dropped = record_asset(session, runs[1], replace(ORDERS, columns=(ID,)), observed_at=T2)
    assert dropped.columns_removed == ["amount"]
    assert _column(session, "amount").deleted_at == T2
    assert all(v.valid_to is not None for v in _column_versions(session, "amount"))

    readded = record_asset(session, runs[2], ORDERS, observed_at=T3)
    column = _column(session, "amount")
    assert readded.columns_added == ["amount"]
    assert column.column_key == key_before
    assert column.deleted_at is None
    versions = _column_versions(session, "amount")
    assert [v.valid_to for v in versions] == [T2, None]


def test_table_level_change_creates_new_asset_version(
    session: Session, runs: tuple[IngestionRun, ...]
) -> None:
    record_asset(session, runs[0], ORDERS, observed_at=T1)
    result = record_asset(session, runs[1], replace(ORDERS, primary_key=()), observed_at=T2)

    assert result.asset_changed
    versions = session.scalars(select(AssetVersion).order_by(AssetVersion.valid_from)).all()
    assert [v.primary_key for v in versions] == [["id"], []]
    assert [v.valid_to for v in versions] == [T2, None]


def test_missing_table_is_marked_deleted_and_can_reappear(
    session: Session, runs: tuple[IngestionRun, ...]
) -> None:
    orders = record_asset(session, runs[0], ORDERS, observed_at=T1)
    customers = record_asset(
        session, runs[0], replace(ORDERS, name="customers", indexes=()), observed_at=T1
    )

    deleted = mark_missing_assets(session, runs[1], [customers.asset_key], observed_at=T2)

    assert deleted == [orders.asset_key]
    asset = session.get_one(Asset, orders.asset_key)
    assert asset.deleted_at == T2
    current = session.scalars(
        select(AssetVersion).where(
            AssetVersion.asset_key == orders.asset_key, AssetVersion.valid_to.is_(None)
        )
    ).all()
    assert current == []

    back = record_asset(session, runs[2], ORDERS, observed_at=T3)
    assert back.asset_reappeared
    assert back.asset_key == orders.asset_key
    assert session.get_one(Asset, orders.asset_key).deleted_at is None


def test_same_table_name_in_two_sources_are_separate_assets(
    session: Session,
    tenant: Tenant,
    source: DataSource,
    make_source: Callable[[Tenant], DataSource],
    make_run: Callable[[DataSource], IngestionRun],
) -> None:
    other_source = make_source(tenant)
    first = record_asset(session, make_run(source), ORDERS, observed_at=T1)
    second = record_asset(session, make_run(other_source), ORDERS, observed_at=T1)

    assert second.asset_created
    assert first.asset_key != second.asset_key


def test_out_of_order_observation_is_rejected(
    session: Session, runs: tuple[IngestionRun, ...]
) -> None:
    record_asset(session, runs[0], ORDERS, observed_at=T2)
    changed = replace(ORDERS, primary_key=())
    with pytest.raises(ValueError, match="time order"):
        record_asset(session, runs[1], changed, observed_at=T1)


def test_duplicate_column_names_are_rejected(
    session: Session, runs: tuple[IngestionRun, ...]
) -> None:
    with pytest.raises(ValueError, match="duplicate column"):
        record_asset(session, runs[0], replace(ORDERS, columns=(ID, ID)), observed_at=T1)
