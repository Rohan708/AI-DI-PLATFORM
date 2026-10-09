"""Row-level outliers computed inside the database: rows whose value is far above the
column's typical value, on a log scale (so a column of prices from 3 to 1,500 is judged
by its spread in orders of magnitude, not in dollars). Only aggregates and the primary
keys of a few offending rows come back; the values themselves stay in the database."""

from sqlalchemy import Connection, text

from ai_data_engineer.ingestion.base import KeyRef, OutlierResult
from ai_data_engineer.ingestion.postgres.profiling import SAMPLE_SEED, quote_ident

# median absolute deviation -> standard deviation for normally distributed data
MAD_TO_SIGMA = 1.4826


def row_outliers(
    conn: Connection,
    column: KeyRef,
    row_ids: tuple[str, ...],
    *,
    sigmas: float,
    min_ratio: float,
    min_spread: float,
    sample_size: int,
    sample_threshold: int,
) -> OutlierResult:
    """Rows with ``ln(value) > ln(median) + max(sigmas * robust spread, ln(min_ratio))``,
    i.e. both statistically extreme and at least ``min_ratio`` times the median."""
    name = f"t.{quote_ident(column.columns[0])}"
    source = f"{quote_ident(column.schema)}.{quote_ident(column.table)} AS t"
    rows = column.estimated_rows
    sampled = rows is not None and rows > sample_threshold
    if sampled and rows:
        percent = min(100.0, sample_threshold / rows * 100)
        source += f" TABLESAMPLE SYSTEM ({percent:.6f}) REPEATABLE ({SAMPLE_SEED})"
    # Identifiers are quoted by quote_ident; the thresholds are bind parameters.
    stats = conn.execute(
        text(
            f"WITH v AS (SELECT ln({name}::double precision) AS lx FROM {source} "  # noqa: S608
            f"WHERE {name} > 0), "
            "s AS (SELECT count(*) AS n, percentile_cont(0.5) WITHIN GROUP (ORDER BY lx) AS med "
            "FROM v), "
            "d AS (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(v.lx - s.med)) AS mad "
            "FROM v, s), "
            "c AS (SELECT s.n, s.med, s.med + greatest(:sigmas * greatest(:k * d.mad, "
            ":min_spread), ln(:min_ratio)) AS cut FROM s, d) "
            "SELECT c.n, exp(c.med) AS median, exp(c.cut) AS cutoff, c.cut, "
            "(SELECT count(*) FROM v WHERE v.lx > c.cut) AS outliers, "
            "(SELECT exp(max(v.lx) - c.med) FROM v) AS max_ratio FROM c"
        ),
        {"sigmas": sigmas, "k": MAD_TO_SIGMA, "min_spread": min_spread, "min_ratio": min_ratio},
    ).one()
    outliers = int(stats.outliers or 0)
    sample: list[dict[str, str]] = []
    if outliers and row_ids:
        selected = ", ".join(f"t.{quote_ident(c)}::text AS {quote_ident(c)}" for c in row_ids)
        found = conn.execute(
            text(
                f"SELECT {selected} FROM {source} WHERE {name} > 0 "  # noqa: S608
                f"AND ln({name}::double precision) > :cut ORDER BY {name} DESC LIMIT :n"
            ),
            {"cut": stats.cut, "n": sample_size},
        ).mappings()
        sample = [dict(row) for row in found]
    return OutlierResult(
        checked_rows=int(stats.n or 0),
        median=float(stats.median) if stats.median is not None else None,
        cutoff=float(stats.cutoff) if stats.cutoff is not None else None,
        outlier_rows=outliers,
        max_ratio=float(stats.max_ratio) if stats.max_ratio is not None else None,
        sample=sample,
        sampled=sampled,
    )
