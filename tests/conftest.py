"""Shared fixtures.

Anything under ``tests/integration/`` is auto-marked ``integration``. Those tests get a
throwaway Postgres 16 container (testcontainers), migrated to the latest schema once
per session; each test runs in a transaction that is rolled back afterwards.
"""

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.orm import Session

from ai_data_engineer.db import create_db_engine
from ai_data_engineer.graph.models import DataSource, IngestionRun, SourceKind, Tenant

REPO_ROOT = Path(__file__).resolve().parent.parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in item.path.relative_to(config.rootpath).parts:
            item.add_marker(pytest.mark.integration)


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def make_alembic_config() -> Callable[[str], Config]:
    return alembic_config


# --- database ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    from testcontainers.community.postgres import PostgresContainer

    try:
        container = PostgresContainer("postgres:16-alpine", driver="psycopg")
        container.start()
    except Exception as exc:
        # In CI a missing Docker daemon is a real failure; locally, skip with a clear reason.
        if os.environ.get("CI"):
            raise
        pytest.skip(f"Docker not available, skipping integration tests ({exc.__class__.__name__})")

    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture(scope="session")
def migrated_engine(postgres_url: str) -> Iterator[Engine]:
    from alembic import command

    command.upgrade(alembic_config(postgres_url), "head")
    engine = create_db_engine(postgres_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    """A session whose work is rolled back after the test. Use ``session.begin_nested()``
    around statements that are expected to fail."""
    with migrated_engine.connect() as connection:
        transaction = connection.begin()
        db_session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield db_session
        finally:
            db_session.close()
            transaction.rollback()


@pytest.fixture(scope="session")
def make_database(postgres_url: str) -> Iterator[Callable[[], str]]:
    """Factory for brand-new, empty databases in the same container (migration tests,
    lab databases). All are dropped at the end of the session."""
    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    created: list[str] = []

    def _make() -> str:
        name = f"db_{uuid.uuid4().hex[:12]}"
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        created.append(name)
        return make_url(postgres_url).set(database=name).render_as_string(hide_password=False)

    try:
        yield _make
    finally:
        with admin.connect() as connection:
            for name in created:
                connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def empty_database_url(make_database: Callable[[], str]) -> str:
    return make_database()


# --- metadata-store objects ----------------------------------------------------------------


@pytest.fixture
def make_tenant(session: Session) -> Callable[[], Tenant]:
    def _make() -> Tenant:
        tenant = Tenant(name=f"tenant-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        return tenant

    return _make


@pytest.fixture
def tenant(make_tenant: Callable[[], Tenant]) -> Tenant:
    return make_tenant()


@pytest.fixture
def make_source(session: Session) -> Callable[[Tenant], DataSource]:
    def _make(owner: Tenant) -> DataSource:
        source = DataSource(
            tenant_id=owner.id, name=f"db-{uuid.uuid4().hex[:8]}", kind=SourceKind.POSTGRES
        )
        session.add(source)
        session.flush()
        return source

    return _make


@pytest.fixture
def source(make_source: Callable[[Tenant], DataSource], tenant: Tenant) -> DataSource:
    return make_source(tenant)


@pytest.fixture
def make_run(session: Session) -> Callable[[DataSource], IngestionRun]:
    def _make(data_source: DataSource) -> IngestionRun:
        run = IngestionRun(tenant_id=data_source.tenant_id, data_source_id=data_source.id)
        session.add(run)
        session.flush()
        return run

    return _make


@pytest.fixture
def run(make_run: Callable[[DataSource], IngestionRun], source: DataSource) -> IngestionRun:
    return make_run(source)
