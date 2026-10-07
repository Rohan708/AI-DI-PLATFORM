"""Build and run the per-table profiling query. All counting happens inside Postgres;
only aggregates come back (plus top values / text min-max when the source allows).

One query per table measures every column at once:

    SELECT count(*), count(col), count(DISTINCT col), min(col)::text, max(col)::text, ...
    FROM "schema"."table" [TABLESAMPLE SYSTEM (p) REPEATABLE (0)]

Large tables (estimated rows above the threshold) are sampled; the fraction is recorded
so detection knows the numbers are estimates.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from ai_data_engineer.graph.models import TypeFamily
from ai_data_engineer.graph.versioning import ColumnObservation
from ai_data_engineer.ingestion.base import ColumnMeasurement, DiscoveredTable
from ai_data_engineer.ingestion.settings import ScanSettings

# Fixed seed so repeated samples of an unchanged table read the same blocks.
SAMPLE_SEED = 0

_ORDERABLE = {
    TypeFamily.INTEGER,
    TypeFamily.DECIMAL,
    TypeFamily.FLOAT,
    TypeFamily.DATE,
    TypeFamily.TIMESTAMP,
    TypeFamily.TIME,
    TypeFamily.INTERVAL,
    TypeFamily.STRING,
}
_NUMERIC = {TypeFamily.INTEGER, TypeFamily.DECIMAL, TypeFamily.FLOAT}
# Types without a usable equality operator (or too costly) get no distinct count.
_NO_DISTINCT = {TypeFamily.JSON, TypeFamily.ARRAY, TypeFamily.BINARY, TypeFamily.OTHER}
# Text-like values count as "raw values" for privacy (numbers and dates don't identify
# anyone on their own).
_VALUE_FAMILIES = {TypeFamily.STRING}
_TOP_VALUE_FAMILIES = {TypeFamily.STRING, TypeFamily.BOOLEAN}


def quote_ident(name: str) -> str:
    """Quote an identifier for use inside ``sqlalchemy.text()`` (which treats ``:x`` as a
    bind parameter, so colons in names are escaped)."""
    return '"' + name.replace('"', '""').replace(":", r"\:") + '"'


def qualified_name(table: DiscoveredTable) -> str:
    return f"{quote_ident(table.observation.schema_name)}.{quote_ident(table.observation.name)}"


@dataclass(frozen=True)
class ProfilePlan:
    sql: str
    sample_fraction: float  # 1.0 = full scan

    @property
    def sampled(self) -> bool:
        return self.sample_fraction < 1.0


def sample_fraction(table: DiscoveredTable, settings: ScanSettings) -> float:
    rows = table.estimated_rows
    if rows is None or rows <= settings.sample_row_threshold:
        return 1.0
    return settings.sample_row_threshold / rows


def from_clause(table: DiscoveredTable, fraction: float) -> str:
    source = qualified_name(table)
    if fraction >= 1.0:
        return source
    return f"{source} TABLESAMPLE SYSTEM ({fraction * 100:.6f}) REPEATABLE ({SAMPLE_SEED})"


def build_profile_query(table: DiscoveredTable, settings: ScanSettings) -> ProfilePlan:
    fraction = sample_fraction(table, settings)
    parts = ["count(*) AS row_count"]
    for i, column in enumerate(table.observation.columns):
        parts += _column_expressions(i, column, settings)
    sql = f"SELECT {', '.join(parts)} FROM {from_clause(table, fraction)}"  # noqa: S608
    return ProfilePlan(sql=sql, sample_fraction=fraction)


def _column_expressions(i: int, column: ColumnObservation, settings: ScanSettings) -> list[str]:
    col = quote_ident(column.name)
    family = column.type_family
    exprs = [f"count({col}) AS c{i}_nonnull"]
    if family not in _NO_DISTINCT:
        exprs.append(f"count(DISTINCT {col}) AS c{i}_distinct")
    if family in _ORDERABLE and (family not in _VALUE_FAMILIES or settings.allow_value_samples):
        exprs.append(f"min({col})::text AS c{i}_min")
        exprs.append(f"max({col})::text AS c{i}_max")
    if family in _NUMERIC:
        exprs.append(f"avg({col})::float8 AS c{i}_mean")
        exprs.append(f"stddev_samp({col})::float8 AS c{i}_stddev")
    if family is TypeFamily.STRING:
        exprs.append(f"avg(length({col}))::float8 AS c{i}_avg_length")
        # Same value up to letter case ("ANNA@X.COM" vs "anna@x.com") = likely duplicates.
        exprs.append(f"count(DISTINCT lower({col})) AS c{i}_distinct_ci")
    return exprs


def run_profile(
    conn: Connection, table: DiscoveredTable, settings: ScanSettings
) -> tuple[ProfilePlan, int, list[ColumnMeasurement]]:
    """Returns the plan used, the number of rows measured, and per-column measurements."""
    plan = build_profile_query(table, settings)
    row = conn.execute(text(plan.sql)).mappings().one()
    measured_rows = int(row["row_count"])

    measurements = []
    for i, column in enumerate(table.observation.columns):
        distinct = row.get(f"c{i}_distinct")
        top_values = None
        if (
            settings.allow_value_samples
            and column.type_family in _TOP_VALUE_FAMILIES
            and distinct is not None
            and 0 < distinct <= settings.top_values_max_distinct
        ):
            top_values = _top_values(conn, table, column, plan.sample_fraction, settings)
        measurements.append(
            ColumnMeasurement(
                name=column.name,
                row_count=measured_rows,
                null_count=measured_rows - int(row[f"c{i}_nonnull"]),
                distinct_count=int(distinct) if distinct is not None else None,
                distinct_is_approx=plan.sampled,
                min_repr=row.get(f"c{i}_min"),
                max_repr=row.get(f"c{i}_max"),
                mean=row.get(f"c{i}_mean"),
                stddev=row.get(f"c{i}_stddev"),
                avg_length=row.get(f"c{i}_avg_length"),
                top_values=top_values,
                distinct_case_insensitive=row.get(f"c{i}_distinct_ci"),
            )
        )
    return plan, measured_rows, measurements


def _top_values(
    conn: Connection,
    table: DiscoveredTable,
    column: ColumnObservation,
    fraction: float,
    settings: ScanSettings,
) -> list[dict[str, Any]]:
    col = quote_ident(column.name)
    sql = (
        f"SELECT {col}::text AS value, count(*) AS n FROM {from_clause(table, fraction)} "  # noqa: S608
        f"WHERE {col} IS NOT NULL GROUP BY 1 ORDER BY n DESC, value LIMIT :limit"
    )
    rows = conn.execute(text(sql), {"limit": settings.top_values_max_distinct})
    return [{"value": r.value, "count": int(r.n)} for r in rows]
