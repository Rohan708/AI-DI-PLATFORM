import json
import logging
from typing import List

from ai_data_engineer.alerting.notifier import BaseNotifier
from ai_data_engineer.graph.models import Finding

logger = logging.getLogger(__name__)

class SlackNotifier(BaseNotifier):
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def _post(self, payload: dict) -> bool:
        if not self.webhook_url:
            logger.warning("Slack webhook URL not configured. Skipping alert.")
            return False
            
        try:
            import requests
            response = requests.post(
                self.webhook_url,
                data=json.dumps(payload),
                headers={'Content-Type': 'application/json'}
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to send Slack alert: {e}")
            return False

    def send_message(self, finding: Finding, owner_channel: str) -> bool:
        payload = {
            "channel": owner_channel,
            "text": f"*{finding.finding_type.name}*\n{finding.title}\n{finding.description}"
        }
        return self._post(payload)

    def send_digest(self, findings: List[Finding], owner_channel: str) -> bool:
        if not findings:
            return False

        lines = [f"• *{f.finding_type.name}*: {f.title}" for f in findings]
        text = f"*Digest Alert: {len(findings)} Findings*\n" + "\n".join(lines)
        
        payload = {
            "channel": owner_channel,
            "text": text
        }
        return self._post(payload)
