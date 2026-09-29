import os
# Set dummy config for tests before any app code is imported
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SNOWFLAKE_ACCOUNT"] = "dummy"
os.environ["SNOWFLAKE_USER"] = "dummy"
os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"] = "dummy"

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from testcontainers.postgres import PostgresContainer
from ai_data_engineer.graph.models import Base

@pytest.fixture(scope="session")
def postgres_container():
    """Start a Postgres container for the entire test session."""
    with PostgresContainer("postgres:15-alpine") as postgres:
        yield postgres

@pytest.fixture(scope="session")
def engine(postgres_container):
    """Create a SQLAlchemy engine connected to the test container."""
    engine = create_engine(postgres_container.get_connection_url())
    # Create all tables
    Base.metadata.create_all(engine)
    yield engine
    # Drop all tables after session
    Base.metadata.drop_all(engine)

@pytest.fixture(scope="function")
def session(engine):
    """Provide a transactional session for each test."""
    connection = engine.connect()
    transaction = connection.begin()
    
    Session = sessionmaker(bind=connection)
    session = Session()
    
    yield session
    
    session.close()
    transaction.rollback()
    connection.close()
