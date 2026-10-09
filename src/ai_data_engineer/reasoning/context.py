"""What the LLM may see about a source: structure and statistics, never row values.

Sent: table and column names, types, primary keys, row counts, null rates, distinct
counts, the relationships we know (with status and confidence), and (unless switched
off) the minimum/maximum of numeric and date columns, which are aggregates.
Never sent: text values of any kind (min/max of text columns, frequent values, samples).
"""

from typing import Any

from ai_data_engineer.discovery.catalog import Catalog, ColumnInfo
from ai_data_engineer.rules.settings import RuleSettings
from ai_data_engineer.rules.spec import NUMERIC, TEMPORAL, Link

RANGE_FAMILIES = NUMERIC | TEMPORAL


def schema_context(
    catalog: Catalog, links: list[tuple[Link, str, float | None]], settings: RuleSettings
) -> dict[str, Any]:
    """``links``: (relationship, status, confidence) for every relationship nobody rejected."""
    tables = sorted(catalog.tables.values(), key=lambda t: t.ref)[: settings.max_tables_in_prompt]
    return {
        "tables": [
            {
                "table": t.ref,
                "kind": t.kind,
                "rows": t.estimated_rows,
                "primary_key": list(t.primary_key),
                "columns": [_column(c, settings) for c in t.columns.values()],
            }
            for t in tables
        ],
        "relationships": [
            {
                "child": f"{child}({', '.join(child_cols)})",
                "parent": f"{parent}({', '.join(parent_cols)})",
                "status": status,
                "confidence": confidence,
            }
            for (child, child_cols, parent, parent_cols), status, confidence in links
        ],
    }


def _column(c: ColumnInfo, settings: RuleSettings) -> dict[str, Any]:
    out: dict[str, Any] = {"name": c.name, "type": c.native_type, "family": c.family.value}
    if c.row_count:
        out["null_rate"] = round((c.null_count or 0) / c.row_count, 4)
    if c.distinct_count is not None:
        out["distinct"] = c.distinct_count
    if settings.share_numeric_ranges and c.family in RANGE_FAMILIES:
        out["min"], out["max"] = c.min_repr, c.max_repr
    return out
