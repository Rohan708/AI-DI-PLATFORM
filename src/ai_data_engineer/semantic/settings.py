"""Defaults for the personal-data check. Global for now, like the other checks."""

from pydantic import BaseModel, Field

from ai_data_engineer.graph.models import Severity


class SemanticSettings(BaseModel):
    sample_rows: int = Field(10_000, gt=0)  # rows read per table (inside the database)
    # A column "holds" a kind when at least this share of its values look like it.
    holds_share: float = Field(0.8, gt=0, le=1)
    # Personal data the column's name doesn't announce is a finding from this many values
    # and this share of the column (one stray email in a notes field is a lead, not noise).
    unexpected_min_values: int = Field(5, ge=1)
    unexpected_min_share: float = Field(0.005, ge=0)
    max_text_columns_per_table: int = Field(60, gt=0)
    severity: Severity = Severity.MEDIUM
    severity_sensitive: Severity = Severity.HIGH  # card numbers, national ids, IBANs


DEFAULT_SEMANTIC_SETTINGS = SemanticSettings()
