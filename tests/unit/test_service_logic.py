"""Stage 4.1 without a database: schedule maths (time zones, daylight saving, downtime),
schedule settings, the health report and JSON logs."""

import json
import logging
from datetime import UTC, date, datetime, time, timedelta

import pytest
from pydantic import ValidationError

from lumida.ingestion.settings import ScanSettings
from lumida.service.health import health_report
from lumida.service.runner import ServiceState, SourceStatus
from lumida.service.schedule import (
    ScheduleError,
    is_due,
    latest_due,
    next_due,
    occurrence,
    parse_run_at,
    zone,
)
from lumida.service.serve import JsonFormatter

BERLIN = zone("Europe/Berlin")
KOLKATA = zone("Asia/Kolkata")
THREE = time(3, 0)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_a_local_time_in_another_zone() -> None:
    # 03:00 in India (UTC+5:30) is 21:30 UTC the evening before.
    assert occurrence(date(2026, 10, 15), THREE, KOLKATA) == utc(2026, 10, 14, 21, 30)


def test_daylight_saving_days() -> None:
    # 29 March 2026: Berlin jumps from 02:00 to 03:00; 02:30 doesn't exist -> runs at 03:00.
    assert occurrence(date(2026, 3, 29), time(2, 30), BERLIN) == utc(2026, 3, 29, 1, 0)
    # 25 October 2026: 02:30 happens twice -> runs once, at the first (summer time) one.
    assert occurrence(date(2026, 10, 25), time(2, 30), BERLIN) == utc(2026, 10, 25, 0, 30)
    # Ordinary days on either side.
    assert occurrence(date(2026, 3, 28), THREE, BERLIN) == utc(2026, 3, 28, 2, 0)
    assert occurrence(date(2026, 3, 30), THREE, BERLIN) == utc(2026, 3, 30, 1, 0)


def test_latest_and_next() -> None:
    now = utc(2026, 10, 14, 12, 0)
    assert latest_due(now, THREE, zone("UTC")) == utc(2026, 10, 14, 3, 0)
    assert next_due(now, THREE, zone("UTC")) == utc(2026, 10, 15, 3, 0)
    early = utc(2026, 10, 14, 2, 59)
    assert latest_due(early, THREE, zone("UTC")) == utc(2026, 10, 13, 3, 0)


def test_when_a_run_is_due() -> None:
    tz, added = zone("UTC"), utc(2026, 10, 1)
    ran_this_morning = utc(2026, 10, 14, 3, 1)
    assert not is_due(utc(2026, 10, 14, 12), THREE, tz, ran_this_morning, added)
    assert is_due(utc(2026, 10, 15, 3, 0), THREE, tz, ran_this_morning, added)
    # Three days of downtime: due, but one run catches up (the next check isn't due).
    assert is_due(utc(2026, 10, 17, 9), THREE, tz, ran_this_morning, added)
    assert not is_due(utc(2026, 10, 17, 9, 5), THREE, tz, utc(2026, 10, 17, 9), added)
    # A source added at noon waits for its first 03:00, not "the 03:00 that just passed".
    new = utc(2026, 10, 14, 12)
    assert not is_due(utc(2026, 10, 14, 13), THREE, tz, None, new)
    assert is_due(utc(2026, 10, 15, 3), THREE, tz, None, new)


def test_schedule_settings() -> None:
    assert ScanSettings().run_at == "03:00"
    assert ScanSettings(run_at="7:5").run_at == "07:05"
    assert ScanSettings(run_at=None).run_at is None
    with pytest.raises(ValidationError):
        ScanSettings(run_at="25:00")
    with pytest.raises(ValidationError):
        ScanSettings(timezone="Mars/Olympus")
    with pytest.raises(ScheduleError):
        parse_run_at("three")


def test_health_report() -> None:
    now = utc(2026, 10, 14, 12)
    state = ServiceState(started_at=now - timedelta(hours=1), heartbeat=now - timedelta(seconds=30))
    state.sources["shop"] = SourceStatus("shop", "03:00 UTC", last_ok=True)
    code, body = health_report(state, 60, now)
    assert (code, body["status"]) == (200, "ok")

    state.sources["billing"] = SourceStatus("billing", "03:00 UTC", last_ok=False)
    code, body = health_report(state, 60, now)
    assert (code, body["status"], body["failing_sources"]) == (200, "degraded", ["billing"])

    state.heartbeat = now - timedelta(minutes=10)  # the loop stopped ticking
    code, body = health_report(state, 60, now)
    assert (code, body["status"]) == (503, "stalled")


def test_json_logs() -> None:
    record = logging.LogRecord(
        "lumida.service", logging.INFO, __file__, 1, "run %s: ok", ("shop",), None
    )
    entry = json.loads(JsonFormatter().format(record))
    assert (entry["level"], entry["message"], entry["logger"]) == (
        "info",
        "run shop: ok",
        "lumida.service",
    )
