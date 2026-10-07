from collections.abc import Callable

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from ai_data_engineer.graph.models import Base

EXPECTED_TABLES = {
    "tenant",
    "data_source",
    "ingestion_run",
    "asset",
    "asset_version",
    "asset_column",
    "asset_column_version",
    "asset_profile",
    "column_profile",
    "relationship",
    "relationship_column",
    "rule",
    "finding",
    "query_join",
}


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()


def test_upgrade_downgrade_upgrade_on_empty_database(
    empty_database_url: str, make_alembic_config: Callable[[str], Config]
) -> None:
    cfg = make_alembic_config(empty_database_url)

    command.upgrade(cfg, "head")
    assert _tables(empty_database_url) == EXPECTED_TABLES

    command.downgrade(cfg, "base")
    assert _tables(empty_database_url) == set()

    command.upgrade(cfg, "head")
    assert _tables(empty_database_url) == EXPECTED_TABLES


def test_models_and_migrations_agree(
    empty_database_url: str, make_alembic_config: Callable[[str], Config]
) -> None:
    """``alembic check`` fails if the models contain changes no migration captures."""
    cfg = make_alembic_config(empty_database_url)
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_models_cover_every_expected_table() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES
