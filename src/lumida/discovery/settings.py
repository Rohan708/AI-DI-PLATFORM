"""Discovery tuning: named constants with documented defaults (see
docs/design/relationship_discovery.md). Nothing here is a rule about anyone's data."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DiscoverySettings:
    # --- evidence gates ---------------------------------------------------------------
    # Below this share of child values found in the parent, a candidate is discarded.
    min_inclusion: float = 0.8
    # Integer keys (1..N) overlap with everything, so they need a name or query-log signal.
    min_name_score_for_integers: float = 0.6
    # Text values are "distinctive" (can match on values alone) from this average length...
    min_distinctive_length: float = 4.0
    # ...and when the child has at least this many distinct values.
    min_child_distinct: int = 5
    # Child and parent text lengths must agree within this relative tolerance.
    length_tolerance: float = 0.5

    # --- confidence formula -------------------------------------------------------------
    weight_inclusion: float = 0.5
    weight_name: float = 0.25
    weight_query_log: float = 0.25
    bonus_distinctive_values: float = 0.2
    max_confidence: float = 0.99
    # Candidates at or above this confidence are stored as "proposed".
    propose_threshold: float = 0.6

    # --- relationship checks --------------------------------------------------------------
    # Orphan checks run on confirmed relationships and on proposed ones at/above this.
    orphan_check_min_confidence: float = 0.9
    # Orphan share at/above which the finding is "high" severity (else "medium").
    orphan_high_severity_fraction: float = 0.05
    orphan_sample_size: int = 20

    # --- cost limits ----------------------------------------------------------------------
    max_values_checked: int = 10_000  # distinct child values per overlap query
    max_inclusion_checks: int = 200  # overlap queries per discovery run
    query_log_limit: int = 1_000  # most-called statements read from the query log


DEFAULT_DISCOVERY_SETTINGS = DiscoverySettings()
