"""When each source's nightly run is due (pure: no database, no clock of its own).

Every source runs once a day at its own local time (``run_at``, e.g. ``03:00``, in its
``timezone``, e.g. ``Asia/Kolkata``); ``run_at = None`` means "never on a schedule".

A run is **due** when the most recent scheduled moment has passed and nothing ran since
then. So a scheduler that was down for three days runs each source **once** when it
comes back, not three times (catch-up runs would only re-judge the same data).

Daylight saving: a local time that doesn't exist that day (clocks jump from 02:00 to
03:00) runs at the first moment after the jump; a time that happens twice runs once,
at its first occurrence.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ScheduleError(ValueError):
    pass


def parse_run_at(text: str) -> time:
    """``"03:00"`` -> 03:00. Hours 00-23, minutes 00-59."""
    try:
        hours, minutes = text.strip().split(":")
        return time(int(hours), int(minutes))
    except (ValueError, TypeError) as exc:
        raise ScheduleError(f"run time must look like 03:00, got {text!r}") from exc


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"unknown time zone {name!r} (e.g. UTC, Europe/Berlin)") from exc


def occurrence(day: date, run_at: time, tz: ZoneInfo) -> datetime:
    """The UTC moment of ``run_at`` on local ``day`` (first occurrence if it repeats; just
    after the jump if it doesn't exist that day)."""
    local = datetime.combine(day, run_at, tzinfo=tz).replace(fold=0)
    moment = local.astimezone(UTC)
    if moment.astimezone(tz).replace(tzinfo=None) != local.replace(tzinfo=None):
        # A skipped local time: run at the first valid minute after it.
        moment = _first_valid_after(local, tz)
    return moment


def latest_due(now: datetime, run_at: time, tz: ZoneInfo) -> datetime:
    """The most recent scheduled moment at or before ``now``."""
    today = now.astimezone(tz).date()
    for day in (today, today - timedelta(days=1), today - timedelta(days=2)):
        moment = occurrence(day, run_at, tz)
        if moment <= now:
            return moment
    raise AssertionError("a daily schedule always has a moment in the last two days")


def next_due(now: datetime, run_at: time, tz: ZoneInfo) -> datetime:
    """The first scheduled moment strictly after ``now``."""
    today = now.astimezone(tz).date()
    for day in (today, today + timedelta(days=1), today + timedelta(days=2)):
        moment = occurrence(day, run_at, tz)
        if moment > now:
            return moment
    raise AssertionError("a daily schedule always has a moment in the next two days")


def is_due(now: datetime, run_at: time, tz: ZoneInfo, last_run: datetime | None,
           added_at: datetime) -> bool:  # fmt: skip
    """Due when a scheduled moment passed after the last run (or, for a source that never
    ran, after it was added: a new source waits for its first scheduled time)."""
    since = last_run or added_at
    return latest_due(now, run_at, tz) > since


def _first_valid_after(local: datetime, tz: ZoneInfo) -> datetime:
    probe = local.replace(tzinfo=None)
    for _ in range(4 * 60):  # gaps are at most a couple of hours
        probe += timedelta(minutes=1)
        candidate = probe.replace(tzinfo=tz)
        if candidate.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == probe:
            return candidate.astimezone(UTC)
    raise AssertionError("no valid local time within four hours")
