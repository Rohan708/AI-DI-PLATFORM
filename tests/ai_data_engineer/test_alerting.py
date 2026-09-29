import pytest
from uuid import uuid4
from unittest.mock import Mock

from ai_data_engineer.alerting.router import AlertRouter
from ai_data_engineer.graph.models import Finding, FindingType, FindingStatus, Owner, OwnsEdge, Asset, SourceSystem
from ai_data_engineer.config import settings

@pytest.fixture
def mock_notifier():
    notifier = Mock()
    notifier.send_message.return_value = True
    notifier.send_digest.return_value = True
    return notifier

def test_alert_router_digest(db_session, mock_notifier):
    # Setup owner and asset
    owner = Owner(name="Test Team", slack_channel="#test-alerts")
    db_session.add(owner)
    asset = Asset(source_system=SourceSystem.SNOWFLAKE, database_name="db", schema_name="schema", table_name="table", asset_type="table")
    db_session.add(asset)
    db_session.commit()
    
    edge = OwnsEdge(owner_id=owner.id, asset_id=asset.id)
    db_session.add(edge)
    db_session.commit()

    # Create findings > digest threshold
    threshold = settings.alert_digest_threshold
    findings = []
    for _ in range(threshold + 1):
        f = Finding(
            asset_id=asset.id,
            finding_type=FindingType.QUALITY_ISSUE,
            status=FindingStatus.OPEN,
            title="Test Finding",
            description="Test Description"
        )
        findings.append(f)

    router = AlertRouter(mock_notifier, db_session)
    router.dispatch(findings)

    # Assert digest was sent once, not individual messages
    mock_notifier.send_digest.assert_called_once()
    mock_notifier.send_message.assert_not_called()
    assert mock_notifier.send_digest.call_args[0][1] == "#test-alerts"

    # Assert alerted_at is updated
    for f in findings:
        assert f.alerted_at is not None

def test_alert_router_individual(db_session, mock_notifier):
    asset_id = uuid4()
    findings = [
        Finding(
            asset_id=asset_id,
            finding_type=FindingType.QUALITY_ISSUE,
            status=FindingStatus.OPEN,
            title="Test Finding",
            description="Test Description"
        )
    ]
    
    router = AlertRouter(mock_notifier, db_session)
    router.dispatch(findings)

    mock_notifier.send_digest.assert_not_called()
    mock_notifier.send_message.assert_called_once()
    assert mock_notifier.send_message.call_args[0][1] == settings.slack_fallback_channel
