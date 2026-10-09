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

from ai_data_engineer.graph.models import Severity

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


def group_score(members: Sequence[Score]) -> Score:
    """Average of the members, with the worst one kept; an empty group is perfectly healthy."""
    if not members:
        return Score(score=MAX_SCORE, min_child=None)
    totals: Counter[str] = Counter()
    for m in members:
        totals.update(m.open_findings)
    return Score(
        score=round(sum(m.score for m in members) / len(members), 1),
        open_findings=dict(sorted(totals.items())),
        min_child=min(m.score for m in members),
    )
