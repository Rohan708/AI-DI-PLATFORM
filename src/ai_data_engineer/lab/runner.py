"""Orchestration: build the lab, advance days, inject scenarios, run whole plans.

Each simulated day is its own transaction. ``on_day_end`` is called after each day is
committed; from Stage 1.3 on, that hook scans the lab with the Postgres adapter, so the
metadata store collects one "nightly" scan per simulated day.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Engine

from ai_data_engineer.lab.answer_key import AnswerKey, ExpectedAnomaly
from ai_data_engineer.lab.scenarios import SCENARIOS
from ai_data_engineer.lab.simulation import build, simulate_next_day
from ai_data_engineer.lab.sizes import DEFAULT_START_DATE
from ai_data_engineer.lab.state import LabState, load_state, save_state

DayHook = Callable[[date], None]

# The lab's "nightly scan" runs at 03:00 the morning after a simulated day, i.e. after
# the nightly ETL (02:00) has finished.
SCAN_HOUR = 3


def lab_scan_time(day: date) -> datetime:
    """When the scan of simulated ``day`` happens, on the simulated calendar."""
    return datetime.combine(day + timedelta(days=1), time(SCAN_HOUR), tzinfo=UTC)


@dataclass(frozen=True)
class Plan:
    name: str
    description: str
    baseline_days: int  # clean days (each scanned) before anything is injected
    scenarios: tuple[str, ...]
    days_after: int  # days simulated (and scanned) after injection


STAGE_ONE_SCENARIOS = tuple(
    name
    for name in SCENARIOS
    if name not in ("ship_before_order", "invoice_total_mismatch")  # need Stage 2 rules
)

PLANS: dict[str, Plan] = {
    "standard": Plan(
        "standard",
        "14 clean scanned days, then every scenario, then 1 more day",
        baseline_days=14,
        scenarios=tuple(SCENARIOS),
        days_after=1,
    ),
    "quick": Plan(
        "quick",
        "3 clean days, Stage-1 scenarios, 1 more day (fast smoke run)",
        baseline_days=3,
        scenarios=STAGE_ONE_SCENARIOS,
        days_after=1,
    ),
}


class UnknownScenarioError(ValueError):
    pass


def build_lab(
    engine: Engine, *, seed: int, size: str, start_date: date = DEFAULT_START_DATE
) -> LabState:
    with engine.begin() as conn:
        return build(conn, seed=seed, size=size, start_date=start_date)


def tick(engine: Engine, days: int = 1, *, on_day_end: DayHook | None = None) -> list[date]:
    simulated = []
    for _ in range(days):
        with engine.begin() as conn:
            day = simulate_next_day(conn, load_state(conn))
        simulated.append(day)
        if on_day_end is not None:
            on_day_end(day)
    return simulated


def inject(engine: Engine, names: Iterable[str]) -> list[ExpectedAnomaly]:
    names = list(names)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        raise UnknownScenarioError(f"unknown scenario(s): {', '.join(unknown)}")
    expected: list[ExpectedAnomaly] = []
    with engine.begin() as conn:
        state = load_state(conn)
        for name in names:
            entries = SCENARIOS[name].apply(conn, state)
            state.injected.extend(entry.to_json() for entry in entries)
            expected.extend(entries)
        save_state(conn, state)
    return expected


def answer_key(engine: Engine) -> AnswerKey:
    with engine.connect() as conn:
        state = load_state(conn)
    return AnswerKey(
        seed=state.seed,
        size=state.size,
        current_day=state.current_day,
        anomalies=tuple(ExpectedAnomaly.from_json(entry) for entry in state.injected),
    )


def run_plan(
    engine: Engine,
    plan_name: str,
    *,
    seed: int,
    size: str,
    on_day_end: DayHook | None = None,
) -> AnswerKey:
    """Build from scratch, simulate clean days, inject, simulate the aftermath."""
    plan = PLANS[plan_name]
    build_lab(engine, seed=seed, size=size)
    tick(engine, plan.baseline_days, on_day_end=on_day_end)
    inject(engine, plan.scenarios)
    tick(engine, plan.days_after, on_day_end=on_day_end)
    return answer_key(engine)
