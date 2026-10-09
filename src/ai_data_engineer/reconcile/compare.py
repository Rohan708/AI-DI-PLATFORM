"""Comparing one table with its copy, from the two sources' latest measurements. Pure:
no database access, so the rules are easy to test and to explain.

Order of judgement:
1. **Row count.** If the copy has more or fewer rows (beyond the tolerance), that is the
   finding, and column comparisons are skipped: with different rows, every total and
   distinct count differs too, and repeating that is noise.
2. **Columns** (same name, any letter case), only when the row counts agree: NULL rate,
   distinct count (skipped when estimated), numeric total (mean x non-null rows), and the
   minimum / maximum of numbers and dates (compared as values, so ``2026-03-15
   09:00:00+00`` equals ``2026-03-15T09:00:00+00:00``).
"""

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from ai_data_engineer.graph.models import TypeFamily
from ai_data_engineer.reconcile.settings import ReconcileSettings

NUMERIC = frozenset({TypeFamily.INTEGER, TypeFamily.DECIMAL, TypeFamily.FLOAT})
_SHORT_OFFSET = re.compile(r"\d\d:\d\d(:\d\d(\.\d+)?)?[+-]\d\d$")  # "...09:00:00+00"
TEMPORAL = frozenset({TypeFamily.DATE, TypeFamily.TIMESTAMP})


@dataclass(frozen=True)
class ColumnSnap:
    name: str
    family: TypeFamily
    column_key: Any = None
    row_count: int | None = None
    null_count: int | None = None
    distinct_count: int | None = None
    distinct_is_approx: bool = False
    min_repr: str | None = None
    max_repr: str | None = None
    mean: float | None = None

    @property
    def non_null(self) -> int | None:
        if self.row_count is None or self.null_count is None:
            return None
        return self.row_count - self.null_count

    @property
    def total(self) -> float | None:
        if self.mean is None or self.non_null is None:
            return None
        return self.mean * self.non_null


@dataclass(frozen=True)
class TableSnap:
    ref: str
    asset_key: Any
    rows: int | None
    rows_estimated: bool
    measured_at: datetime | None
    columns: dict[str, ColumnSnap] = field(default_factory=dict)  # by lower-case name


@dataclass(frozen=True)
class Difference:
    metric: str  # row_count, null_rate, distinct_count, total, minimum, maximum
    column: str | None  # the left table's column name
    left: Any
    right: Any
    detail: str  # one line with the numbers


@dataclass
class TableComparison:
    differences: list[Difference] = field(default_factory=list)
    checked: list[tuple[str, str | None]] = field(default_factory=list)  # (metric, column)
    columns_skipped: bool = False  # row counts differ, so columns weren't compared


def compare(left: TableSnap, right: TableSnap, s: ReconcileSettings) -> TableComparison:
    out = TableComparison()
    if left.rows is not None and right.rows is not None:
        out.checked.append(("row_count", None))
        gap = right.rows - left.rows
        tolerance = (
            s.estimate_row_tolerance
            if left.rows_estimated or right.rows_estimated
            else s.row_tolerance
        )
        if abs(gap) >= s.min_row_difference and abs(gap) / max(left.rows, 1) > tolerance:
            word = "fewer" if gap < 0 else "more"
            out.differences.append(
                Difference(
                    "row_count", None, left.rows, right.rows,
                    f"{right.ref} has {abs(gap):,} {word} rows than {left.ref} "
                    f"({right.rows:,} vs {left.rows:,})",
                )
            )  # fmt: skip
            out.columns_skipped = True
            return out
    for key, lc in left.columns.items():
        rc = right.columns.get(key)
        if rc is not None:
            out.differences += _columns(left, right, lc, rc, s, out.checked)
    return out


def _columns(
    left: TableSnap,
    right: TableSnap,
    lc: ColumnSnap,
    rc: ColumnSnap,
    s: ReconcileSettings,
    checked: list[tuple[str, str | None]],
) -> list[Difference]:
    found: list[Difference] = []
    where = f"{left.ref}.{lc.name} vs {right.ref}.{rc.name}"

    def rate(c: ColumnSnap) -> float | None:
        return c.null_count / c.row_count if c.row_count and c.null_count is not None else None

    lr, rr = rate(lc), rate(rc)
    if lr is not None and rr is not None:
        checked.append(("null_rate", lc.name))
        if abs(lr - rr) > s.null_rate_tolerance and lc.null_count != rc.null_count:
            found.append(Difference("null_rate", lc.name, lr, rr,
                                    f"NULLs {lr:.2%} vs {rr:.2%} ({where})"))  # fmt: skip

    if (
        lc.distinct_count is not None
        and rc.distinct_count is not None
        and not (lc.distinct_is_approx or rc.distinct_is_approx)
    ):
        checked.append(("distinct_count", lc.name))
        gap = abs(lc.distinct_count - rc.distinct_count)
        if gap and gap / max(lc.distinct_count, 1) > s.distinct_tolerance:
            found.append(Difference(
                "distinct_count", lc.name, lc.distinct_count, rc.distinct_count,
                f"{lc.distinct_count:,} vs {rc.distinct_count:,} distinct values ({where})",
            ))  # fmt: skip

    if lc.family in NUMERIC and rc.family in NUMERIC:
        lt, rt = lc.total, rc.total
        if lt is not None and rt is not None:
            checked.append(("total", lc.name))
            if abs(lt - rt) > s.sum_tolerance * max(abs(lt), 1.0):
                found.append(Difference("total", lc.name, round(lt, 4), round(rt, 4),
                                        f"total {lt:,.2f} vs {rt:,.2f} ({where})"))  # fmt: skip

    if (lc.family in NUMERIC and rc.family in NUMERIC) or (
        lc.family in TEMPORAL and rc.family in TEMPORAL
    ):
        for metric, a, b in (("minimum", lc.min_repr, rc.min_repr),
                             ("maximum", lc.max_repr, rc.max_repr)):  # fmt: skip
            va, vb = comparable(a, lc.family), comparable(b, rc.family)
            if va is None or vb is None:
                continue
            checked.append((metric, lc.name))
            if not same(va, vb):
                found.append(Difference(metric, lc.name, a, b, f"{metric} {a} vs {b} ({where})"))
    return found


def comparable(value: str | None, family: TypeFamily) -> float | datetime | date | None:
    """A measured extreme as a value: numbers as floats, dates and times as (UTC, naive)
    datetimes, so different databases' spellings of the same moment compare equal."""
    if value is None:
        return None
    if family in NUMERIC:
        try:
            return float(value)
        except ValueError:
            return None
    text = value.strip().replace("T", " ", 1)
    if _SHORT_OFFSET.search(text):  # Postgres prints "+00"; Python wants "+00:00"
        text += ":00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment


def same(a: float | datetime | date, b: float | datetime | date) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return a == b

