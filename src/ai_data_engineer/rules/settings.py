"""Defaults for business rules and AI rule proposals. Global for now (per-source overrides
are in the backlog, as for discovery)."""

from pydantic import BaseModel

from ai_data_engineer.graph.models import Severity


class RuleSettings(BaseModel):
    # --- running approved rules -----------------------------------------------------------
    sample_size: int = 20  # identifiers of offending rows kept as evidence
    high_severity_fraction: float = 0.01  # >= 1% of judged rows break it: high, else medium
    severity_high: Severity = Severity.HIGH
    severity_default: Severity = Severity.MEDIUM

    # --- AI proposals ---------------------------------------------------------------------
    min_confidence: float = 0.5  # proposals the AI itself rates lower are dropped
    max_proposals: int = 40  # per request; more is noise for a reviewer
    max_tables_in_prompt: int = 200
    # Numeric/date minimum and maximum are aggregates, shared by default so the AI can
    # suggest ranges. Text values (min/max, frequent values) are never sent.
    share_numeric_ranges: bool = True


DEFAULT_RULE_SETTINGS = RuleSettings()
