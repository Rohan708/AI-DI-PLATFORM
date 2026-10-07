"""What a detection check is and returns.

Two kinds of checks:
- **event**: something *happened* (a column was dropped). The finding stays open until a
  person resolves it; the change doesn't "un-happen" on the next scan.
- **condition**: something *is currently true* (null rate is abnormally high). The finding
  is resolved automatically once a later run evaluates the subject and it's fine again.

Each check reports the fingerprints it *evaluated*, so a condition is only resolved for
subjects that were actually re-checked (a check still learning doesn't resolve anything).
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ai_data_engineer.detection.context import DetectionContext
from ai_data_engineer.graph.models import FindingCategory, Severity

CheckKind = Literal["event", "condition"]


@dataclass(frozen=True)
class Observation:
    fingerprint: str
    category: FindingCategory
    severity: Severity
    title: str
    description: str
    evidence: dict[str, Any]
    asset_key: uuid.UUID | None = None
    column_key: uuid.UUID | None = None
    relationship_id: uuid.UUID | None = None


@dataclass
class CheckOutput:
    observations: list[Observation] = field(default_factory=list)
    evaluated: set[str] = field(default_factory=set)
    learning: int = 0  # subjects skipped for lack of history (cold start)


@dataclass(frozen=True)
class Check:
    name: str
    kind: CheckKind
    description: str
    run: Callable[[DetectionContext], CheckOutput]
