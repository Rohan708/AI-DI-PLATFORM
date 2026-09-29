import abc
from typing import List

from ai_data_engineer.graph.models import Finding

class BaseNotifier(abc.ABC):
    @abc.abstractmethod
    def send_message(self, finding: Finding, owner_channel: str) -> bool:
        """Send a single alert message."""
        pass

    @abc.abstractmethod
    def send_digest(self, findings: List[Finding], owner_channel: str) -> bool:
        """Send a digested alert message for multiple findings."""
        pass
