"""The lab's own bookkeeping, stored in ``aide_lab.state`` inside the lab database:
seed, size, the current simulated day, id counters, pending ETL faults, and the
answer key of everything injected so far."""

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import Connection, text

STATE_KEY = "state"

INITIAL_IDS = {
    "customer": 1,
    "address": 1,
    "order": 1,
    "payment": 1,
    "invoice": 700_001,  # legacy invoice numbers don't overlap order ids
    "legacy_customer": 9_000_001,  # legacy-only customers
}


class LabNotBuiltError(RuntimeError):
    pass


@dataclass
class LabState:
    seed: int
    size: str
    start_date: date
    current_day: date  # last simulated day that has been completed
    next_ids: dict[str, int] = field(default_factory=lambda: dict(INITIAL_IDS))
    # ETL faults to apply on the next simulated day, e.g. {"skip_reporting_load": True}.
    pending_faults: dict[str, Any] = field(default_factory=dict)
    # Answer-key entries (ExpectedAnomaly.to_json()) for everything injected so far.
    injected: list[dict[str, Any]] = field(default_factory=list)

    def take(self, counter: str) -> int:
        """Next id from a counter (ids are generated here so data is reproducible)."""
        value = self.next_ids[counter]
        self.next_ids[counter] = value + 1
        return value

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["start_date"] = self.start_date.isoformat()
        data["current_day"] = self.current_day.isoformat()
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "LabState":
        return cls(
            seed=data["seed"],
            size=data["size"],
            start_date=date.fromisoformat(data["start_date"]),
            current_day=date.fromisoformat(data["current_day"]),
            next_ids=dict(data["next_ids"]),
            pending_faults=dict(data["pending_faults"]),
            injected=list(data["injected"]),
        )


def load_state(conn: Connection) -> LabState:
    exists = conn.scalar(text("SELECT to_regclass('aide_lab.state') IS NOT NULL"))
    value = (
        conn.scalar(text("SELECT value FROM aide_lab.state WHERE key = :k"), {"k": STATE_KEY})
        if exists
        else None
    )
    if value is None:
        raise LabNotBuiltError("the lab database is not built yet; run `aide lab build`")
    return LabState.from_json(value)


def save_state(conn: Connection, state: LabState) -> None:
    conn.execute(
        text(
            "INSERT INTO aide_lab.state (key, value) VALUES (:k, CAST(:v AS jsonb)) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        ),
        {"k": STATE_KEY, "v": json.dumps(state.to_json())},
    )
