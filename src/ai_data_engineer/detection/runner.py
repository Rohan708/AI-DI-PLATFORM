import logging
from uuid import UUID
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import select, and_

from ai_data_engineer.graph.models import Finding, FindingStatus
from ai_data_engineer.detection.rules import get_all_checks
from ai_data_engineer.config import settings

# Newly added
from ai_data_engineer.alerting.router import AlertRouter
from ai_data_engineer.alerting.slack_notifier import SlackNotifier
from ai_data_engineer.health_score.engine import HealthScoreEngine

logger = logging.getLogger(__name__)


def run_all_checks(session: Session, asset_id: UUID | None = None) -> None:
    """
    Run all registered deterministic checks against the metadata graph.
    Writes new findings to the database, deduplicating against existing OPEN findings.
    
    Args:
        session: SQLAlchemy session object
        asset_id: Optional UUID to limit checks to a specific asset
    """
    checks = get_all_checks()
    logger.info(f"Running {len(checks)} detection checks...")

    total_findings = 0
    new_findings_list = []
    for check in checks:
        logger.info(f"Running check: {check.__class__.__name__}")
        
        try:
            results = check.run(session, asset_id=asset_id)
            
            for res in results:
                total_findings += 1
                
                # Deduplication query
                # Look for an identical OPEN finding for the same asset/column and title
                existing_query = select(Finding).where(
                    and_(
                        Finding.finding_type == res.finding_type,
                        Finding.status == FindingStatus.OPEN,
                        Finding.asset_id == res.asset_id,
                        Finding.column_id == res.column_id,
                        Finding.title == res.title
                    )
                )
                existing = session.scalars(existing_query).first()
                
                if existing:
                    # Deduplicate: Update existing finding's evidence and timestamp
                    logger.debug(f"Updating existing finding: {res.title}")
                    existing.evidence = res.evidence
                    existing.detected_at = datetime.utcnow()
                    existing.description = res.description # optionally update desc too
                else:
                    # Create new finding
                    logger.debug(f"Creating new finding: {res.title}")
                    new_finding = Finding(
                        finding_type=res.finding_type,
                        status=FindingStatus.OPEN,
                        asset_id=res.asset_id,
                        column_id=res.column_id,
                        job_id=res.job_id,
                        title=res.title,
                        description=res.description,
                        confidence=None, # deterministic
                        evidence=res.evidence,
                    )
                    session.add(new_finding)
                    new_findings_list.append(new_finding)
                    
        except Exception as e:
            logger.error(f"Check {check.__class__.__name__} failed: {e}", exc_info=True)
            
    session.commit()
    logger.info(f"Detection run complete. Generated/Updated {total_findings} findings.")
    
    # Update Health Scores based on all OPEN findings
    logger.info("Computing health scores...")
    try:
        engine = HealthScoreEngine(session)
        engine.compute_health_scores()
    except Exception as e:
        logger.error(f"Health score computation failed: {e}", exc_info=True)

    # Route new alerts
    if new_findings_list:
        logger.info(f"Dispatching alerts for {len(new_findings_list)} new findings...")
        try:
            notifier = SlackNotifier(webhook_url=settings.slack_webhook_url or "")
            router = AlertRouter(notifier, session)
            router.dispatch(new_findings_list)
        except Exception as e:
            logger.error(f"Alert dispatch failed: {e}", exc_info=True)
