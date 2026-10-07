"""Detection tuning: named constants with documented defaults (docs/design/detection.md).
None of these are facts about anyone's data; they set how surprising a change must be."""

from dataclasses import dataclass

from ai_data_engineer.graph.models import Severity


@dataclass(frozen=True)
class DetectionSettings:
    # --- history ------------------------------------------------------------------------
    baseline_scans: int = 14  # how many earlier scans form "normal"
    min_history_scans: int = 7  # fewer than this: statistical checks say "still learning"
    event_lookback_days: int = 7  # structural changes this recent become findings

    # --- statistics -----------------------------------------------------------------------
    # Robust z-score: (value - median) / (1.4826 * MAD), with a floor on the spread so a
    # perfectly flat history doesn't make every tiny wobble "infinitely" unusual.
    robust_z_threshold: float = 4.0
    null_rate_min_spread: float = 0.005  # 0.5 percentage points
    null_rate_min_increase: float = 0.02  # and the rise must be at least 2 points
    null_rate_high_increase: float = 0.2  # >= 20 points: high severity
    magnitude_jump_factor: float = 10.0  # max suddenly 10x the largest value ever seen

    # --- volume / freshness -------------------------------------------------------------
    volume_drop_ratio: float = 0.6  # new rows below 60% of the usual for that weekday
    volume_min_baseline_rows: int = 5  # ignore tables that normally gain fewer rows
    # One earlier scan on the same weekday beats an all-days median that ignores the weekly
    # pattern (seen 2026-10-07: a quiet Monday flagged against the all-days median).
    min_same_weekday_samples: int = 1  # else compare with all days
    freshness_min_regularity: float = 0.8  # column advanced in >= 80% of earlier scans

    # --- severities ----------------------------------------------------------------------
    severity_column_removed: Severity = Severity.HIGH
    severity_type_family_changed: Severity = Severity.HIGH
    severity_type_detail_changed: Severity = Severity.LOW  # e.g. varchar(20) -> varchar(50)
    severity_nullability_changed: Severity = Severity.LOW
    severity_primary_key_removed: Severity = Severity.HIGH
    severity_missing_primary_key: Severity = Severity.MEDIUM
    severity_unindexed_foreign_key: Severity = Severity.LOW
    severity_value_anomaly: Severity = Severity.MEDIUM
    severity_duplicates: Severity = Severity.HIGH
    severity_volume_drop: Severity = Severity.HIGH
    severity_stale: Severity = Severity.HIGH


DEFAULT_DETECTION_SETTINGS = DetectionSettings()
