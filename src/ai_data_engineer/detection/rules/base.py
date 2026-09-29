import abc
from typing import Any
from dataclasses import dataclass
from sqlalchemy.orm import Session
from uuid import UUID

from ai_data_engineer.graph.models import FindingType


@dataclass
class FindingResult:
    """A data class representing a pending finding before it is written to the database."""
    title: str
    description: str
    finding_type: FindingType
    evidence: dict[str, Any]
    asset_id: UUID | None = None
    column_id: UUID | None = None
    job_id: UUID | None = None


class BaseCheck(abc.ABC):
    """
    Abstract base class for all deterministic detection rules.
    Checks must implement the run() method and yield zero or more FindingResult instances.
    """

    @abc.abstractmethod
    def run(self, session: Session, asset_id: UUID | None = None) -> list[FindingResult]:
        """
        Execute the check.
        
        Args:
            session: SQLAlchemy session
            asset_id: If provided, restrict the check to this specific asset. Otherwise, run on the whole graph.
            
        Returns:
            A list of FindingResult objects representing detected issues.
        """
        pass


# Global registry of all registered checks
_CHECK_REGISTRY: list[BaseCheck] = []


def register_check(check: BaseCheck) -> None:
    """Register a check instance to be run by the detection runner."""
    _CHECK_REGISTRY.append(check)


def get_all_checks() -> list[BaseCheck]:
    """Retrieve all registered checks."""
    return _CHECK_REGISTRY
