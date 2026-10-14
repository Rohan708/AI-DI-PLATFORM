"""Bring the metadata store up to date, using the migrations shipped inside the package
(so it works from an installed wheel or the Docker image, without ``alembic.ini``)."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def upgrade_to_head(database_url: str) -> str | None:
    """Apply any pending migrations; returns the revision the store is now at."""
    command.upgrade(alembic_config(database_url), "head")
    return current_revision(database_url)


def current_revision(database_url: str) -> str | None:
    engine = create_engine(database_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()
