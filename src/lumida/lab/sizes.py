"""How big the lab database is. ``tiny`` is for tests, ``default`` for benchmarks."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class LabSize:
    name: str
    customers: int  # created at build time (more arrive daily)
    products: int
    history_days: int  # simulated days of activity created by ``build``
    orders_per_day: int  # baseline, before weekday seasonality and noise


SIZES: dict[str, LabSize] = {
    # 30 orders a day, not fewer: below that a planted half load (40%) can't be told from
    # a slow day once count noise is allowed for (detection's volume_noise_sigmas).
    "tiny": LabSize("tiny", customers=60, products=20, history_days=20, orders_per_day=30),
    "small": LabSize("small", customers=600, products=80, history_days=60, orders_per_day=40),
    "default": LabSize(
        "default", customers=3000, products=200, history_days=90, orders_per_day=150
    ),
}

# Business calendar: the first simulated day of activity.
DEFAULT_START_DATE = date(2026, 1, 1)
