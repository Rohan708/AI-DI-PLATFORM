from sqlalchemy import text

from ai_data_engineer.db import create_db_engine


def test_can_connect_to_postgres(postgres_url: str) -> None:
    engine = create_db_engine(postgres_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar_one() == 1
            server_version = str(conn.execute(text("SHOW server_version")).scalar_one())
    finally:
        engine.dispose()

    assert server_version.startswith("16")
