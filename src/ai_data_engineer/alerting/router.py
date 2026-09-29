from collections import defaultdict
from datetime import datetime
from typing import List
import logging

from sqlalchemy.orm import Session
from ai_data_engineer.alerting.notifier import BaseNotifier
from ai_data_engineer.graph.models import Finding, OwnsEdge, Owner
from ai_data_engineer.config import settings

logger = logging.getLogger(__name__)

class AlertRouter:
    def __init__(self, notifier: BaseNotifier, session: Session):
        self.notifier = notifier
        self.session = session

    def _resolve_owner_channel(self, asset_id) -> str:
        if not asset_id:
            return settings.slack_fallback_channel
            
        edge = self.session.query(OwnsEdge).filter_by(asset_id=asset_id).first()
        if edge:
            owner = self.session.query(Owner).filter_by(id=edge.owner_id).first()
            if owner and owner.slack_channel:
                return owner.slack_channel
                
        return settings.slack_fallback_channel

    def dispatch(self, findings: List[Finding]) -> None:
        """
        Groups findings by asset_id and dispatches alerts.
        If a single asset has more findings than alert_digest_threshold, 
        sends a digest. Otherwise, sends individual messages.
        Updates the alerted_at timestamp for processed findings.
        """
        # Group by asset_id
        grouped = defaultdict(list)
        for f in findings:
            grouped[f.asset_id].append(f)

        now = datetime.utcnow()

        for asset_id, asset_findings in grouped.items():
            channel = self._resolve_owner_channel(asset_id)
            
            success = False
            if len(asset_findings) >= settings.alert_digest_threshold:
                success = self.notifier.send_digest(asset_findings, channel)
            else:
                success_count = 0
                for f in asset_findings:
                    if self.notifier.send_message(f, channel):
                        success_count += 1
                success = success_count == len(asset_findings)
            
            if success:
                for f in asset_findings:
                    f.alerted_at = now
                self.session.commit()
            else:
                logger.error(f"Failed to dispatch alerts for asset_id={asset_id}")
