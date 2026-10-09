"""Per-pair reconciliation settings, stored as JSON in ``reconciliation_pair.settings``.
Every value has a documented default; unknown keys are rejected."""

from pydantic import BaseModel, ConfigDict, Field


class ReconcileSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Name mapping when the copy uses other names. Explicit tables win over schema_map;
    # with explicit tables, a table missing from the copy is a finding.
    schema_map: dict[str, str] = Field(default_factory=dict)  # {"shop": "analytics"}
    tables: dict[str, str] = Field(default_factory=dict)  # {"shop.orders": "dw.fct_orders"}

    # Row counts: relative difference allowed (0.001 = 0.1%), and a looser one when a
    # count is an estimate (large tables are sampled); smaller absolute gaps are ignored.
    row_tolerance: float = Field(0.001, ge=0)
    estimate_row_tolerance: float = Field(0.05, ge=0)
    min_row_difference: int = Field(1, ge=1)
    # Columns (compared only when the row counts agree; otherwise every column differs).
    null_rate_tolerance: float = Field(0.001, ge=0)  # absolute: 0.001 = 0.1 points
    distinct_tolerance: float = Field(0.001, ge=0)  # relative
    sum_tolerance: float = Field(1e-6, ge=0)  # relative, for numeric totals
    # Scans further apart than this aren't compared (the copy may simply be behind).
    max_scan_gap_hours: float = Field(26.0, gt=0)
