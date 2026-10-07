"""Time-series checks on table-level history: volume and freshness."""

import uuid
from dataclasses import dataclass
from itertools import pairwise
from statistics import median

from ai_data_engineer.detection.context import DetectionContext, fingerprint
from ai_data_engineer.detection.framework import Check, CheckOutput, Observation
from ai_data_engineer.graph.models import AssetProfile, FindingCategory, TypeFamily

VOLUME_DROP = "volume_drop"
STALE_TABLE = "stale_table"

TIME_FAMILIES = (TypeFamily.TIMESTAMP, TypeFamily.DATE)


@dataclass(frozen=True)
class _Clock:
    """The timestamp column that best shows when a table was last filled."""

    regularity: float  # share of earlier scans in which it moved forward
    name: str
    column_key: uuid.UUID
    previous: str  # its maximum at the previous scan
    latest: str  # its maximum now


def volume_drop(ctx: DetectionContext) -> CheckOutput:
    """Rows added since the previous scan far below the usual, compared with the same
    weekday when there is enough history (weekends are naturally quieter)."""
    s, out = ctx.settings, CheckOutput()
    for table in ctx.catalog.tables.values():
        history, current = ctx.asset_series(table.asset_key)
        series = [p for p in [*history, current] if p is not None]
        if current is None or any(p.row_count is None or p.row_count_is_estimate for p in series):
            continue
        deltas = _deltas(series)  # (profile, rows added since the previous scan)
        if len(deltas) < 2:
            out.learning += 1
            continue
        *past, (_, added) = deltas
        if not ctx.enough_history(past):
            out.learning += 1
            continue
        if any(d < 0 for _, d in past):
            continue  # tables that shrink (full refreshes, purges) aren't append-like
        baseline_values, basis = volume_baseline(
            [(p.measured_at.weekday(), d) for p, d in past],
            current.measured_at.weekday(),
            s.min_same_weekday_samples,
        )
        usual = median(baseline_values)
        fp = fingerprint(VOLUME_DROP, table.asset_key)
        if usual < s.volume_min_baseline_rows:
            continue
        out.evaluated.add(fp)
        if added >= s.volume_drop_ratio * usual:
            continue
        out.observations.append(
            Observation(
                fingerprint=fp,
                category=FindingCategory.TIME_SERIES,
                severity=s.severity_volume_drop,
                title=f"{table.ref} gained {added:,} rows; usually about {usual:,.0f}",
                description=(
                    f"Since the previous scan {table.ref} gained {added:,} rows, "
                    f"{added / usual:.0%} of the usual {usual:,.0f} "
                    f"(median of {len(baseline_values)} scans on {basis}). "
                    "A load may have failed or been partial."
                ),
                evidence={
                    "rows_added": added,
                    "usual_rows_added": usual,
                    "ratio": round(added / usual, 3),
                    "threshold_ratio": s.volume_drop_ratio,
                    "baseline": basis,
                    "baseline_samples": baseline_values,
                },
                asset_key=table.asset_key,
            )
        )
    return out


def stale_table(ctx: DetectionContext) -> CheckOutput:
    """A table whose timestamp/date column advanced on (almost) every earlier scan didn't
    advance this time: the job that fills it probably didn't run."""
    s, out = ctx.settings, CheckOutput()
    for table in ctx.catalog.tables.values():
        best: _Clock | None = None
        enough = False
        for column in table.columns.values():
            if column.family not in TIME_FAMILIES:
                continue
            history, current = ctx.column_series(column.column_key)
            if current is None or current.max_repr is None:
                continue
            values = [p.max_repr for p in history if p.max_repr is not None]
            if not ctx.enough_history(values):
                continue
            enough = True
            advanced = sum(1 for a, b in pairwise(values) if b > a)
            regularity = advanced / (len(values) - 1)
            if regularity < s.freshness_min_regularity:
                continue
            if best is None or regularity > best.regularity:
                best = _Clock(
                    regularity, column.name, column.column_key, values[-1], current.max_repr
                )
        if not enough:
            out.learning += 1
            continue
        fp = fingerprint(STALE_TABLE, table.asset_key)
        if best is None:
            continue  # no column that normally moves: freshness can't be judged
        out.evaluated.add(fp)
        if best.latest > best.previous:
            continue
        name, latest = best.name, best.latest
        out.observations.append(
            Observation(
                fingerprint=fp,
                category=FindingCategory.TIME_SERIES,
                severity=s.severity_stale,
                title=f"{table.ref} stopped updating: {name} still {latest}",
                description=(
                    f"{table.ref}.{name} moved forward in {best.regularity:.0%} of the previous "
                    "scans, "
                    f"but not since the last one (still {latest}). The process that fills "
                    f"{table.ref} probably didn't run."
                ),
                evidence={
                    "column": name,
                    "latest_value": latest,
                    "previous_value": best.previous,
                    "regularity": round(best.regularity, 3),
                    "threshold_regularity": s.freshness_min_regularity,
                },
                asset_key=table.asset_key,
                column_key=best.column_key,
            )
        )
    return out


def volume_baseline(
    past: list[tuple[int, int]], weekday: int, min_same_weekday: int
) -> tuple[list[int], str]:
    """Earlier row increases to compare with: those on the same weekday if there are
    enough (weekly seasonality), otherwise all of them. ``past`` is (weekday, rows added)."""
    same_day = [added for day, added in past if day == weekday]
    if len(same_day) >= min_same_weekday:
        return same_day, "the same weekday"
    return [added for _, added in past], "recent scans"


def _deltas(series: list[AssetProfile]) -> list[tuple[AssetProfile, int]]:
    return [
        (cur, (cur.row_count or 0) - (prev.row_count or 0))
        for prev, cur in pairwise(series)
    ]


CHECKS = [
    Check(VOLUME_DROP, "condition", "far fewer new rows than usual", volume_drop),
    Check(STALE_TABLE, "condition", "a regularly updated table stopped updating", stale_table),
]
