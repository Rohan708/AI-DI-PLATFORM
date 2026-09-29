import uuid
from datetime import datetime
from ai_data_engineer.graph.models import Asset, AssetColumn, SourceSystem, Finding, FindingType
from ai_data_engineer.detection.runner import run_all_checks


def test_runner_deduplication(session):
    asset_id = uuid.uuid4()
    
    # Create asset with data type drift
    asset_v1 = Asset(
        id=uuid.uuid4(), source_system=SourceSystem.SNOWFLAKE, 
        database_name="run_db", schema_name="sch", table_name="tbl", 
        asset_type="table", valid_from=datetime(2023, 1, 1), valid_to=datetime(2023, 1, 2)
    )
    c1 = AssetColumn(asset_id=asset_v1.id, column_name="col1", data_type="INT", valid_from=asset_v1.valid_from, valid_to=asset_v1.valid_to)
    
    asset_v2 = Asset(
        id=asset_id, source_system=SourceSystem.SNOWFLAKE, 
        database_name="run_db", schema_name="sch", table_name="tbl", 
        asset_type="table", valid_from=datetime(2023, 1, 2), valid_to=None
    )
    c2 = AssetColumn(asset_id=asset_v2.id, column_name="col1", data_type="VARCHAR", valid_from=asset_v2.valid_from, valid_to=None)
    
    session.add_all([asset_v1, c1, asset_v2, c2])
    session.commit()
    
    # First run
    run_all_checks(session, asset_id=asset_id)
    
    findings = session.query(Finding).filter(Finding.asset_id == asset_id).all()
    assert len(findings) == 1
    first_detected_at = findings[0].detected_at
    
    # Second run (should deduplicate)
    run_all_checks(session, asset_id=asset_id)
    
    findings = session.query(Finding).filter(Finding.asset_id == asset_id).all()
    assert len(findings) == 1  # Still 1
    assert findings[0].detected_at > first_detected_at  # Timestamp updated
