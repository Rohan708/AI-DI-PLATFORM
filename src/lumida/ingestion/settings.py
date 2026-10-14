"""Per-source scan settings, stored as JSON in ``data_source.settings``.

Every value has a documented default, so an empty ``{}`` is a valid configuration.
Unknown keys are rejected (a typo should fail loudly, not be silently ignored).
"""

from pydantic import BaseModel, ConfigDict, Field

# Every query we run is cancelled by the database after this long.
DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
# Never wait long for a lock held by the customer's application.
DEFAULT_LOCK_TIMEOUT_MS = 2_000
# Tables estimated above this many rows are profiled on a sample of about this size.
DEFAULT_SAMPLE_ROW_THRESHOLD = 1_000_000
# Top values are collected only for columns with at most this many distinct values.
DEFAULT_TOP_VALUES_MAX_DISTINCT = 50


class ScanSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Empty = all schemas. Exclusions win over inclusions.
    include_schemas: tuple[str, ...] = ()
    exclude_schemas: tuple[str, ...] = ()
    statement_timeout_ms: int = Field(DEFAULT_STATEMENT_TIMEOUT_MS, gt=0)
    lock_timeout_ms: int = Field(DEFAULT_LOCK_TIMEOUT_MS, gt=0)
    sample_row_threshold: int = Field(DEFAULT_SAMPLE_ROW_THRESHOLD, gt=0)
    top_values_max_distinct: int = Field(DEFAULT_TOP_VALUES_MAX_DISTINCT, ge=0)
    # Whether actual values may be stored: top values and min/max of text columns.
    # On by default for self-hosted installs (values stay in the customer's network).
    allow_value_samples: bool = True

    def includes_schema(self, schema: str) -> bool:
        if schema in self.exclude_schemas:
            return False
        return not self.include_schemas or schema in self.include_schemas
