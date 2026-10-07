"""Column-value checks: each column compared with its own history (no fixed thresholds
about the data). All need ``min_history_scans`` earlier scans; until then they "learn"."""

import re
from statistics import median

from ai_data_engineer.detection.context import DetectionContext, fingerprint
from ai_data_engineer.detection.framework import Check, CheckOutput, Observation
from ai_data_engineer.detection.stats import robust_z
from ai_data_engineer.discovery.catalog import ColumnInfo, TableInfo
from ai_data_engineer.graph.models import ColumnProfile, FindingCategory, Severity, TypeFamily

NULL_RATE_SPIKE = "null_rate_spike"
OUT_OF_RANGE = "out_of_range"
INCONSISTENT_CATEGORIES = "inconsistent_categories"
DUPLICATE_VALUES = "duplicate_values"

NUMERIC = (TypeFamily.INTEGER, TypeFamily.DECIMAL, TypeFamily.FLOAT)
KEYLIKE = (TypeFamily.STRING, TypeFamily.INTEGER, TypeFamily.UUID)


def _columns(ctx: DetectionContext) -> list[tuple[TableInfo, ColumnInfo]]:
    return [(t, c) for t in ctx.catalog.tables.values() for c in t.columns.values()]


def null_rate_spike(ctx: DetectionContext) -> CheckOutput:
    s, out = ctx.settings, CheckOutput()
    for table, column in _columns(ctx):
        history, current = ctx.column_series(column.column_key)
        if current is None or current.null_rate is None:
            continue
        past = [p.null_rate for p in history if p.null_rate is not None]
        if not ctx.enough_history(past):
            out.learning += 1
            continue
        fp = fingerprint(NULL_RATE_SPIKE, column.column_key)
        out.evaluated.add(fp)
        baseline = median(past)
        increase = current.null_rate - baseline
        z = robust_z(current.null_rate, past, s.null_rate_min_spread)
        if increase < s.null_rate_min_increase or z < s.robust_z_threshold:
            continue
        ref = f"{table.ref}.{column.name}"
        out.observations.append(
            Observation(
                fingerprint=fp,
                category=FindingCategory.COLUMN_VALUE,
                severity=Severity.HIGH
                if increase >= s.null_rate_high_increase
                else s.severity_value_anomaly,
                title=f"NULLs in {ref} jumped from {baseline:.1%} to {current.null_rate:.1%}",
                description=(
                    f"{ref} is normally {baseline:.1%} NULL (median of the last {len(past)} "
                    f"scans) and is now {current.null_rate:.1%}: {current.null_count:,} of "
                    f"{current.row_count:,} rows. That is {z:.0f} typical spreads above normal."
                ),
                evidence={
                    "baseline_null_rate": baseline,
                    "current_null_rate": current.null_rate,
                    "null_rows": current.null_count,
                    "rows": current.row_count,
                    "robust_z": round(z, 2),
                    "threshold_z": s.robust_z_threshold,
                    "min_increase": s.null_rate_min_increase,
                    "history_scans": len(past),
                },
                asset_key=table.asset_key,
                column_key=column.column_key,
            )
        )
    return out


def out_of_range(ctx: DetectionContext) -> CheckOutput:
    """Values outside anything seen before: negatives in an always-positive column, or a
    maximum suddenly ``magnitude_jump_factor`` times the largest ever seen."""
    s, out = ctx.settings, CheckOutput()
    for table, column in _columns(ctx):
        if column.family not in NUMERIC:
            continue
        history, current = ctx.column_series(column.column_key)
        if current is None:
            continue
        mins = [v for v in (_num(p.min_repr) for p in history) if v is not None]
        maxs = [v for v in (_num(p.max_repr) for p in history) if v is not None]
        cur_min, cur_max = _num(current.min_repr), _num(current.max_repr)
        if not ctx.enough_history(mins) or cur_min is None or cur_max is None:
            out.learning += 1
            continue
        fp = fingerprint(OUT_OF_RANGE, column.column_key)
        out.evaluated.add(fp)
        reasons = []
        if min(mins) >= 0 > cur_min:
            reasons.append(
                f"negative values appeared (minimum {cur_min:g}; was never below {min(mins):g})"
            )
        if max(maxs) > 0 and cur_max > s.magnitude_jump_factor * max(maxs):
            reasons.append(f"maximum jumped to {cur_max:g} (largest before: {max(maxs):g})")
        if not reasons:
            continue
        ref = f"{table.ref}.{column.name}"
        out.observations.append(
            Observation(
                fingerprint=fp,
                category=FindingCategory.COLUMN_VALUE,
                severity=s.severity_value_anomaly,
                title=f"Unusual values in {ref}: {reasons[0]}",
                description=f"{ref}: "
                + "; ".join(reasons)
                + f" (compared with {len(mins)} earlier scans).",
                evidence={
                    "current_min": cur_min,
                    "current_max": cur_max,
                    "historical_min": min(mins),
                    "historical_max": max(maxs),
                    "reasons": reasons,
                },
                asset_key=table.asset_key,
                column_key=column.column_key,
            )
        )
    return out


def inconsistent_categories(ctx: DetectionContext) -> CheckOutput:
    """New values in a low-cardinality column whose set of values was stable, flagged as
    variants when they look like an existing value ('USA', 'United States' vs 'US')."""
    out = CheckOutput()
    for table, column in _columns(ctx):
        if column.family is not TypeFamily.STRING:
            continue
        history, current = ctx.column_series(column.column_key)
        if current is None or current.top_values is None:
            continue
        past = [p for p in history if p.top_values is not None]
        if not ctx.enough_history(past):
            out.learning += 1
            continue
        if len({p.distinct_count for p in past}) != 1:
            continue  # the set of values was still changing: not a stable category list
        fp = fingerprint(INCONSISTENT_CATEGORIES, column.column_key)
        out.evaluated.add(fp)
        known = {str(v["value"]) for p in past for v in p.top_values or []}
        new = {
            str(v["value"]): int(v["count"])
            for v in current.top_values
            if str(v["value"]) not in known
        }
        if not new:
            continue
        variants = {
            value: sorted(k for k in known if looks_like_variant(value, k)) for value in new
        }
        ref = f"{table.ref}.{column.name}"
        listed = ", ".join(f"'{v}' ({n:,} rows)" for v, n in sorted(new.items()))
        similar = {v: ks for v, ks in variants.items() if ks}
        title = (
            f"Inconsistent values in {ref}: {listed} look like variants of "
            + ", ".join(sorted({f"'{k}'" for ks in similar.values() for k in ks}))
            if similar
            else f"New values in {ref}: {listed}"
        )
        out.observations.append(
            Observation(
                fingerprint=fp,
                category=FindingCategory.COLUMN_VALUE,
                severity=ctx.settings.severity_value_anomaly,
                title=title,
                description=(
                    f"{ref} had the same {len(known)} values in the last {len(past)} scans "
                    f"({', '.join(sorted(known))}); now it also has {listed}."
                ),
                evidence={
                    "known_values": sorted(known),
                    "new_values": new,
                    "looks_like": similar,
                    "history_scans": len(past),
                },
                asset_key=table.asset_key,
                column_key=column.column_key,
            )
        )
    return out


def duplicate_values(ctx: DetectionContext) -> CheckOutput:
    """A column whose values were always unique now has duplicates, including values that
    differ only by letter case ("ANNA@X.COM" vs "anna@x.com")."""
    out = CheckOutput()
    for table, column in _columns(ctx):
        if column.family not in KEYLIKE:
            continue
        history, current = ctx.column_series(column.column_key)
        if current is None or current.sample_fraction < 1.0:
            continue
        past = [p for p in history if p.sample_fraction == 1.0]
        if not ctx.enough_history(past):
            out.learning += 1
            continue
        if not all(_unique(p) for p in past):
            continue  # never unique: duplicates are normal here
        fp = fingerprint(DUPLICATE_VALUES, column.column_key)
        out.evaluated.add(fp)
        non_null = _non_null(current)
        exact = non_null - (current.distinct_count or 0)
        ci = current.extra.get("distinct_case_insensitive")
        case_dupes = non_null - ci if isinstance(ci, int) else 0
        if exact <= 0 and case_dupes <= 0:
            continue
        ref = f"{table.ref}.{column.name}"
        what = (
            f"{exact:,} exact duplicate values"
            if exact > 0
            else f"{case_dupes:,} values that differ from another only by letter case"
        )
        out.observations.append(
            Observation(
                fingerprint=fp,
                category=FindingCategory.RELATIONAL,  # duplicate entities (taxonomy 2)
                severity=ctx.settings.severity_duplicates,
                title=f"Duplicates in {ref}: {what}",
                description=(
                    f"{ref} was unique in each of the last {len(past)} scans; now {non_null:,} "
                    f"values have {what}. This usually means the same entity was created twice."
                ),
                evidence={
                    "non_null_values": non_null,
                    "distinct_values": current.distinct_count,
                    "distinct_case_insensitive": ci,
                    "exact_duplicates": exact,
                    "case_only_duplicates": case_dupes,
                    "history_scans": len(past),
                },
                asset_key=table.asset_key,
                column_key=column.column_key,
            )
        )
    return out


def looks_like_variant(value: str, known: str) -> bool:
    """Same thing written differently: equal ignoring case/punctuation, one a prefix of the
    other ('USA' / 'US'), or initials of a multi-word value ('United States' / 'US')."""
    a, b = _norm(value), _norm(known)
    if not a or not b or a == b:
        return bool(a) and a == b
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 2 and long_.startswith(short):
        return True
    for multi, other in ((value, b), (known, a)):
        words = re.findall(r"[A-Za-z0-9]+", multi)
        if len(words) > 1 and "".join(w[0] for w in words).lower() == other:
            return True
    return False


def _norm(value: str) -> str:
    return re.sub(r"[^0-9a-z]", "", value.lower())


def _num(text: str | None) -> float | None:
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


def _non_null(p: ColumnProfile) -> int:
    return (p.row_count or 0) - (p.null_count or 0)


def _unique(p: ColumnProfile) -> bool:
    non_null = _non_null(p)
    ci = p.extra.get("distinct_case_insensitive")
    return non_null > 0 and p.distinct_count == non_null and (ci is None or ci == non_null)


CHECKS = [
    Check(
        NULL_RATE_SPIKE, "condition", "share of NULLs far above its usual level", null_rate_spike
    ),
    Check(OUT_OF_RANGE, "condition", "values outside anything seen before", out_of_range),
    Check(
        INCONSISTENT_CATEGORIES,
        "condition",
        "new or inconsistent values in a stable category column",
        inconsistent_categories,
    ),
    Check(
        DUPLICATE_VALUES,
        "condition",
        "a column that was unique now has duplicates",
        duplicate_values,
    ),
]
