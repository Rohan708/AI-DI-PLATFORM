import pytest

from ai_data_engineer.health_score.calculator import calculate_score
from ai_data_engineer.health_score.engine import HealthScoreEngine
from ai_data_engineer.graph.models import FindingType, Asset, SourceSystem, Finding, FindingStatus, HealthScoreSnapshot, HealthScoreScope
from ai_data_engineer.config import settings

def test_calculator_pure_function():
    # Base 100 - quality(20) - schema(15) = 65
    findings = [FindingType.QUALITY_ISSUE, FindingType.SCHEMA_DRIFT]
    score = calculate_score(findings)
    assert score == 65.0

    # Test bounds (min 0)
    findings = [FindingType.QUALITY_ISSUE] * 6 # 6 * 20 = 120
    score = calculate_score(findings)
    assert score == 0.0

def test_engine_integration(db_session):
    # Create two assets in the same schema
    asset1 = Asset(source_system=SourceSystem.SNOWFLAKE, database_name="db", schema_name="sch", table_name="t1", asset_type="table")
    asset2 = Asset(source_system=SourceSystem.SNOWFLAKE, database_name="db", schema_name="sch", table_name="t2", asset_type="table")
    db_session.add_all([asset1, asset2])
    db_session.commit()

    # Create findings for asset1: Quality (-20)
    f1 = Finding(asset_id=asset1.id, finding_type=FindingType.QUALITY_ISSUE, status=FindingStatus.OPEN, title="F1", description="D1")
    # Create findings for asset2: Cost (-5)
    f2 = Finding(asset_id=asset2.id, finding_type=FindingType.COST_INEFFICIENCY, status=FindingStatus.OPEN, title="F2", description="D2")
    db_session.add_all([f1, f2])
    db_session.commit()

    # Compute health scores
    engine = HealthScoreEngine(db_session)
    engine.compute_health_scores()

    # Verify ASSET scores
    asset_scores = db_session.query(HealthScoreSnapshot).filter_by(scope=HealthScoreScope.ASSET).all()
    assert len(asset_scores) == 2
    
    a1_score = next(s for s in asset_scores if s.asset_id == asset1.id).score
    a2_score = next(s for s in asset_scores if s.asset_id == asset2.id).score
    assert a1_score == 80.0
    assert a2_score == 95.0

    # Verify SCHEMA scores
    schema_scores = db_session.query(HealthScoreSnapshot).filter_by(scope=HealthScoreScope.SCHEMA).all()
    assert len(schema_scores) == 1
    s_score = schema_scores[0]
    assert s_score.score == (80.0 + 95.0) / 2 # 87.5
    assert s_score.min_child_score == 80.0

    # Verify GLOBAL scores
    global_scores = db_session.query(HealthScoreSnapshot).filter_by(scope=HealthScoreScope.GLOBAL).all()
    assert len(global_scores) == 1
    g_score = global_scores[0]
    assert g_score.score == 87.5
    assert g_score.min_child_score == 80.0
