"""Turn a candidate's evidence into a confidence, and pick the best explanation per child.

    confidence = w_inclusion * inclusion_term          (how many child rows exist in the parent)
               + w_name      * name_score              (does the name point at the parent?)
               + w_query     * [app joins them]        (query log)
               + bonus       * [distinctive values]    (e.g. 'CU00000017' codes, not 1..N)
    capped at max_confidence; inclusion_term = 0 at min_inclusion, 1 at 100%.

Pure functions; weights live in ``DiscoverySettings``.
"""

from ai_data_engineer.discovery.candidates import Candidate
from ai_data_engineer.discovery.settings import DiscoverySettings


def score(candidate: Candidate, settings: DiscoverySettings) -> float:
    """Sets ``candidate.confidence`` and human-readable ``reasons``; returns the confidence."""
    c, s = candidate, settings
    if c.inclusion is None:
        c.reasons.append("no child rows to check")
        c.confidence = 0.0
        return 0.0
    c.reasons.append(f"{c.inclusion:.1%} of {c.values_checked} child rows exist in the parent")
    if c.inclusion < s.min_inclusion:
        c.reasons.append(f"below the {s.min_inclusion:.0%} minimum: rejected")
        c.confidence = 0.0
        return 0.0

    inclusion_term = (c.inclusion - s.min_inclusion) / (1.0 - s.min_inclusion)
    confidence = s.weight_inclusion * inclusion_term + s.weight_name * c.name_score
    if c.name_score:
        c.reasons.append(f"name match score {c.name_score:.2f}")
    if c.query_calls:
        confidence += s.weight_query_log
        c.reasons.append(f"the application joins these columns ({c.query_calls} calls)")
    if c.distinctive_values:
        confidence += s.bonus_distinctive_values
        c.reasons.append("values are distinctive codes, not small numbers")
    if c.key_source == "historical_key":
        c.reasons.append("parent key existed in an earlier version of the table")
    c.confidence = round(min(confidence, s.max_confidence), 3)
    return c.confidence


def choose_best(accepted: list[Candidate]) -> list[Candidate]:
    """Keep one explanation per child column set and drop redundant ones:

    1. each child column (set) points to at most one parent: the most confident
       (ties: better name match);
    2. a 1:1 pair found in both directions keeps only the more confident direction;
    3. single-column links covered by an accepted composite link from the same child
       columns are dropped (INV_LINE_TAX.INV_NO is part of its link to INV_LINE).
    """
    best: dict[tuple[str, tuple[str, ...]], Candidate] = {}
    for c in sorted(accepted, key=lambda c: (c.confidence, c.name_score), reverse=True):
        best.setdefault((c.child.ref, c.child_columns), c)

    kept: list[Candidate] = []
    seen_pairs: set[frozenset[tuple[str, tuple[str, ...]]]] = set()
    for c in sorted(best.values(), key=lambda c: c.confidence, reverse=True):
        pair = frozenset({(c.child.ref, c.child_columns), (c.parent.ref, c.parent_columns)})
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        kept.append(c)

    composite_columns = {
        (c.child.ref, column) for c in kept if c.is_composite for column in c.child_columns
    }
    return [
        c
        for c in kept
        if c.is_composite or (c.child.ref, c.child_columns[0]) not in composite_columns
    ]
