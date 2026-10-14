"""The health-score formula: pure functions, no database.

    table score  = max(0, 100 - sum of penalties of its open findings)
    schema score = average of its tables' scores   (+ the worst table, kept separately)
    source score = average of all tables' scores   (+ the worst table)

The worst table is kept next to every average, so one broken critical table can't hide
behind 40 healthy ones.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from lumida.graph.models import Severity

MAX_SCORE = 100.0

# Points deducted per open finding, by severity. Diagnostic-only findings (info) are free.
SEVERITY_PENALTY: dict[Severity, float] = {
    Severity.CRITICAL: 40.0,
    Severity.HIGH: 20.0,
    Severity.MEDIUM: 10.0,
    Severity.LOW: 3.0,
    Severity.INFO: 0.0,
}


@dataclass(frozen=True)
class Score:
    score: float
    open_findings: dict[str, int] = field(default_factory=dict)  # severity -> count
    min_child: float | None = None  # for groups: the worst member


def table_score(severities: Iterable[Severity]) -> Score:
    counts = Counter(severities)
    penalty = sum(SEVERITY_PENALTY[s] * n for s, n in counts.items())
    return Score(
        score=max(0.0, MAX_SCORE - penalty),
        open_findings={s.value: n for s, n in sorted(counts.items(), key=lambda kv: kv[0].value)},
    )


# How much a table counts in its schema's and source's average, by the importance people
# gave it (Stage 2.7: 1 low, 2 normal, 3 critical). Normal tables count once.
CRITICALITY_WEIGHT: dict[int, float] = {1: 0.5, 2: 1.0, 3: 2.0}


def group_score(members: Sequence[Score], weights: Sequence[float] | None = None) -> Score:
    """Weighted average of the members (equal weights by default), with the worst one
    kept; an empty group is perfectly healthy."""
    if not members:
        return Score(score=MAX_SCORE, min_child=None)
    w = list(weights) if weights is not None else [1.0] * len(members)
    totals: Counter[str] = Counter()
    for m in members:
        totals.update(m.open_findings)
    return Score(
        score=round(sum(m.score * x for m, x in zip(members, w, strict=True)) / sum(w), 1),
        open_findings=dict(sorted(totals.items())),
        min_child=min(m.score for m in members),
    )
