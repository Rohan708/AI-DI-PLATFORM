from ai_data_engineer.graph.models import Asset, SourceSystem

def test_database_connection(session):
    """Verify that the test database and models are working."""
    new_asset = Asset(
        source_system=SourceSystem.SNOWFLAKE,
        database_name="TEST_DB",
        schema_name="TEST_SCHEMA",
        table_name="TEST_TABLE",
        asset_type="table"
    )
    session.add(new_asset)
    session.commit()
    
    fetched = session.query(Asset).filter_by(table_name="TEST_TABLE").first()
    assert fetched is not None
    assert fetched.database_name == "TEST_DB"
