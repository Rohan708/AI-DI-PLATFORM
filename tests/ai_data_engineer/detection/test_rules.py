from datetime import datetime, timedelta
import uuid
from ai_data_engineer.graph.models import Asset, AssetColumn, SourceSystem, FindingType
from ai_data_engineer.detection.rules import NullRateSpikeCheck, TypeDriftCheck, SchemaDriftCheck, DistinctCountAnomalyCheck


def test_type_drift_check(session):
    # Setup
    asset_id = uuid.uuid4()
    past_date = datetime.utcnow() - timedelta(days=1)
    
    asset_v1 = Asset(
        id=uuid.uuid4(), source_system=SourceSystem.SNOWFLAKE, 
        database_name="db", schema_name="sch", table_name="tbl", 
        asset_type="table", valid_from=past_date - timedelta(days=1), valid_to=past_date
    )
    col_v1 = AssetColumn(
        asset_id=asset_v1.id, column_name="age", data_type="INTEGER", valid_from=past_date - timedelta(days=1), valid_to=past_date
    )
    
    asset_v2 = Asset(
        id=asset_id, source_system=SourceSystem.SNOWFLAKE, 
        database_name="db", schema_name="sch", table_name="tbl", 
        asset_type="table", valid_from=past_date, valid_to=None
    )
    col_v2 = AssetColumn(
        asset_id=asset_v2.id, column_name="age", data_type="VARCHAR", valid_from=past_date, valid_to=None
    )
    
    session.add_all([asset_v1, col_v1, asset_v2, col_v2])
    session.commit()
    
    # Execute
    check = TypeDriftCheck()
    findings = check.run(session, asset_id=asset_id)
    
    # Assert
    assert len(findings) == 1
    assert findings[0].finding_type == FindingType.SCHEMA_DRIFT
    assert findings[0].evidence["previous_type"] == "INTEGER"
    assert findings[0].evidence["current_type"] == "VARCHAR"


def test_schema_drift_check(session):
    asset_id = uuid.uuid4()
    past_date = datetime.utcnow() - timedelta(days=1)
    
    asset_v1 = Asset(
        id=uuid.uuid4(), source_system=SourceSystem.SNOWFLAKE, 
        database_name="db2", schema_name="sch", table_name="tbl2", 
        asset_type="table", valid_from=past_date - timedelta(days=1), valid_to=past_date
    )
    # V1 has "id" and "dropped_col"
    c1_v1 = AssetColumn(asset_id=asset_v1.id, column_name="id", data_type="INTEGER", valid_from=past_date - timedelta(days=1), valid_to=past_date)
    c2_v1 = AssetColumn(asset_id=asset_v1.id, column_name="dropped_col", data_type="VARCHAR", valid_from=past_date - timedelta(days=1), valid_to=past_date)
    
    asset_v2 = Asset(
        id=asset_id, source_system=SourceSystem.SNOWFLAKE, 
        database_name="db2", schema_name="sch", table_name="tbl2", 
        asset_type="table", valid_from=past_date, valid_to=None
    )
    # V2 has "id" and "added_col"
    c1_v2 = AssetColumn(asset_id=asset_v2.id, column_name="id", data_type="INTEGER", valid_from=past_date, valid_to=None)
    c3_v2 = AssetColumn(asset_id=asset_v2.id, column_name="added_col", data_type="VARCHAR", valid_from=past_date, valid_to=None)
    
    session.add_all([asset_v1, c1_v1, c2_v1, asset_v2, c1_v2, c3_v2])
    session.commit()
    
    check = SchemaDriftCheck()
    findings = check.run(session, asset_id=asset_id)
    
    assert len(findings) == 2
    change_types = [f.evidence["change_type"] for f in findings]
    assert "column_added" in change_types
    assert "column_dropped" in change_types


def test_null_rate_spike_check(session):
    asset_id = uuid.uuid4()
    base_date = datetime.utcnow()
    
    # Create 3 historical versions with ~0.05 null rate
    for i in range(3, 0, -1):
        a = Asset(
            id=uuid.uuid4(), source_system=SourceSystem.SNOWFLAKE, database_name="db3", schema_name="sch", table_name="tbl3", 
            asset_type="table", valid_from=base_date - timedelta(days=i), valid_to=base_date - timedelta(days=i-1)
        )
        c = AssetColumn(asset_id=a.id, column_name="email", data_type="VARCHAR", null_rate=0.05, valid_from=a.valid_from, valid_to=a.valid_to)
        session.add_all([a, c])
    
    # Active version with 0.20 null rate (spike > 0.10)
    a_active = Asset(
        id=asset_id, source_system=SourceSystem.SNOWFLAKE, database_name="db3", schema_name="sch", table_name="tbl3", 
        asset_type="table", valid_from=base_date, valid_to=None
    )
    c_active = AssetColumn(asset_id=a_active.id, column_name="email", data_type="VARCHAR", null_rate=0.20, valid_from=a_active.valid_from, valid_to=None)
    session.add_all([a_active, c_active])
    session.commit()
    
    check = NullRateSpikeCheck()
    findings = check.run(session, asset_id=asset_id)
    
    assert len(findings) == 1
    assert findings[0].finding_type == FindingType.QUALITY_ISSUE
    assert findings[0].evidence["current_null_rate"] == 0.20
    assert findings[0].evidence["baseline_avg"] == 0.05
