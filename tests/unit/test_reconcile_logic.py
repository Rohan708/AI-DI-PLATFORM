"""Reconciliation rules without a database: what counts as a difference, and what doesn't."""

from datetime import UTC, datetime

from ai_data_engineer.graph.models import TypeFamily
from ai_data_engineer.reconcile.compare import ColumnSnap, TableSnap, comparable, compare
from ai_data_engineer.reconcile.settings import ReconcileSettings

T = datetime(2026, 3, 16, 3, tzinfo=UTC)
S = ReconcileSettings()


def _table(ref: str, rows: int, *columns: ColumnSnap, estimated: bool = False) -> TableSnap:
    return TableSnap(ref, None, rows, estimated, T, {c.name.lower(): c for c in columns})


def _amount(**kw: object) -> ColumnSnap:
    values: dict[str, object] = {
        "name": "total_amount", "family": TypeFamily.DECIMAL, "row_count": 1000,
        "null_count": 0, "distinct_count": 900, "min_repr": "3.00", "max_repr": "1500.00",
        "mean": 210.5,
    }  # fmt: skip
    return ColumnSnap(**{**values, **kw})  # type: ignore[arg-type]


def test_identical_tables_agree_whatever_the_spelling() -> None:
    left = _table("shop.orders", 1000, _amount(),
                  ColumnSnap("order_date", TypeFamily.TIMESTAMP, row_count=1000, null_count=0,
                             max_repr="2026-03-15 09:00:00+00"))  # fmt: skip
    right = _table("analytics.ORDERS", 1000, _amount(name="TOTAL_AMOUNT", max_repr="1500"),
                   ColumnSnap("ORDER_DATE", TypeFamily.TIMESTAMP, row_count=1000, null_count=0,
                              max_repr="2026-03-15T09:00:00+00:00"))  # fmt: skip
    # column names are matched case-insensitively by the caller's dict keys
    right = TableSnap(right.ref, None, 1000, False, T,
                      {"total_amount": right.columns["total_amount"],
                       "order_date": right.columns["order_date"]})  # fmt: skip
    result = compare(left, right, S)
    assert result.differences == []
    assert ("maximum", "order_date") in result.checked


def test_missing_rows_are_one_finding_not_a_cascade() -> None:
    left = _table("shop.orders", 1000, _amount())
    right = _table("dw.orders", 993, _amount(row_count=993, mean=211.0, distinct_count=894))
    result = compare(left, right, S)
    assert [d.metric for d in result.differences] == ["row_count"]
    assert "7 fewer rows" in result.differences[0].detail
    assert result.columns_skipped


def test_column_differences_when_row_counts_agree() -> None:
    left = _table("shop.orders", 1000, _amount())
    right = _table("dw.orders", 1000, _amount(null_count=80, mean=200.0, distinct_count=820))
    metrics = {d.metric for d in compare(left, right, S).differences}
    assert {"null_rate", "distinct_count", "total"} <= metrics


def test_estimated_counts_get_a_looser_tolerance() -> None:
    left = _table("shop.events", 10_000_000, estimated=True)
    close = _table("dw.events", 10_200_000, estimated=True)  # 2%: within the 5% for estimates
    far = _table("dw.events", 11_000_000, estimated=True)
    assert compare(left, close, S).differences == []
    assert compare(left, far, S).differences[0].metric == "row_count"


def test_comparable_values() -> None:
    assert comparable("2026-03-15 09:00:00+00", TypeFamily.TIMESTAMP) == comparable(
        "2026-03-15 11:00:00+02:00", TypeFamily.TIMESTAMP
    )
    # compared as naive UTC datetimes, so a date and its midnight are equal
    assert comparable("2026-03-15", TypeFamily.DATE) == datetime(2026, 3, 15)  # noqa: DTZ001
    assert comparable("12.50", TypeFamily.DECIMAL) == 12.5
    assert comparable("not a date", TypeFamily.DATE) is None
