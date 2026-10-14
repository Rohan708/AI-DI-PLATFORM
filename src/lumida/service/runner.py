"""The scheduler's work: once per tick, run every source whose nightly time has come.

- **One at a time**, so a customer's databases never see several Lumida jobs at once.
- **Never twice**: a Postgres advisory lock per source on the metadata store means two
  Lumida containers (or a manual ``lumida run``) can't run the same source together.
- **Never in a loop**: an attempt counts as a run even if it failed before recording
  anything (e.g. a missing connection), so a broken source is retried the next night,
  not every minute.
- **Self-monitoring**: when a run fails, that is itself alerted, on the source's own
  channel ("Lumida's run for shopco failed at scan: ...").
"""

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from lumida.alerting.notifier import AlertMessage, Notifier, notifier_for
from lumida.graph.models import DataSource, IngestionRun, utcnow
from lumida.ingestion.scan import AdapterFactory, open_adapter
from lumida.ingestion.sources import list_sources, scan_settings
from lumida.pipeline import run_pipeline
from lumida.service.schedule import is_due, next_due, parse_run_at, zone

log = logging.getLogger("lumida.service")


@dataclass
class SourceStatus:
    name: str
    schedule: str  # "03:00 Europe/Berlin" or "not scheduled"
    last_run_at: datetime | None = None
    last_ok: bool | None = None
    last_summary: str = ""
    next_run_at: datetime | None = None


@dataclass
class ServiceState:
    """What the health endpoint reports. Updated by the scheduler loop."""

    started_at: datetime = field(default_factory=utcnow)
    heartbeat: datetime | None = None
    sources: dict[str, SourceStatus] = field(default_factory=dict)
    attempts: dict[uuid.UUID, datetime] = field(default_factory=dict)  # in-memory, see above


@dataclass(frozen=True)
class RunReport:
    source: str
    ok: bool
    summary: str
    skipped: str | None = None  # why it didn't run (e.g. "already running elsewhere")


def run_due(
    engine: Engine,
    state: ServiceState,
    *,
    now: datetime | None = None,
    adapter_factory: AdapterFactory = open_adapter,
    notifier_factory: Callable[[DataSource], Notifier] = notifier_for,
) -> list[RunReport]:
    now = now or utcnow()
    state.heartbeat = now
    reports = []
    with Session(engine) as session:
        sources = [s for s in list_sources(session) if s.disabled_at is None]
        session.expunge_all()
    for source in sources:
        report = _maybe_run(engine, state, source, now, adapter_factory, notifier_factory)
        if report is not None:
            reports.append(report)
    return reports


def _maybe_run(
    engine: Engine,
    state: ServiceState,
    source: DataSource,
    now: datetime,
    adapter_factory: AdapterFactory,
    notifier_factory: Callable[[DataSource], Notifier],
) -> RunReport | None:
    settings = scan_settings(source)
    status = state.sources.setdefault(source.name, SourceStatus(source.name, "not scheduled"))
    if settings.run_at is None:
        status.schedule, status.next_run_at = "not scheduled", None
        return None
    run_at, tz = parse_run_at(settings.run_at), zone(settings.timezone)
    status.schedule = f"{settings.run_at} {settings.timezone}"
    recorded = _last_run(engine, source.id)
    attempted = state.attempts.get(source.id)
    last = max((t for t in (recorded, attempted) if t is not None), default=None)
    status.last_run_at = status.last_run_at or recorded
    if not is_due(now, run_at, tz, last, source.created_at):
        status.next_run_at = next_due(now, run_at, tz)
        return None

    with source_lock(engine, source.id) as locked:
        if not locked:
            log.info("run %s: skipped, already running elsewhere", source.name)
            return RunReport(source.name, True, "", skipped="already running elsewhere")
        state.attempts[source.id] = now
        log.info("run %s: starting", source.name)
        ok, summary = _run(engine, source, adapter_factory)
    status.last_run_at, status.last_ok, status.last_summary = now, ok, summary
    status.next_run_at = next_due(now, run_at, tz)
    log.info("run %s: %s", source.name, "ok" if ok else "FAILED")
    if not ok:
        _alert_failure(source, summary, notifier_factory)
    return RunReport(source.name, ok, summary)


def _run(engine: Engine, source: DataSource, adapter_factory: AdapterFactory) -> tuple[bool, str]:
    """One pipeline run in its own session. Anything unexpected is reported, not raised:
    one broken source must not stop the others."""
    try:
        with Session(engine) as session:
            fresh = session.get_one(DataSource, source.id)
            result = run_pipeline(session, fresh, adapter_factory=adapter_factory)
            session.commit()
            return result.ok, result.summary()
    except Exception as exc:  # the scheduler must survive anything one run does
        log.exception("run %s: crashed", source.name)
        return False, f"lumida run {source.name}: crashed ({type(exc).__name__}: {exc})"[:500]


def _alert_failure(
    source: DataSource, summary: str, notifier_factory: Callable[[DataSource], Notifier]
) -> None:
    lines = [line.strip() for line in summary.splitlines()[1:] if line.strip()]
    message = AlertMessage(
        title=f"Lumida: the nightly run for {source.name} failed",
        lines=lines or [summary],
    )
    try:
        notifier_factory(source).send(message)
    except Exception:  # a broken alert channel is logged; it must not stop the scheduler
        log.exception("run %s: could not send the failure alert", source.name)


def _last_run(engine: Engine, source_id: uuid.UUID) -> datetime | None:
    with Session(engine) as session:
        return session.scalar(
            select(IngestionRun.started_at)
            .where(IngestionRun.data_source_id == source_id)
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )


@contextmanager
def source_lock(engine: Engine, source_id: uuid.UUID) -> Iterator[bool]:
    """A Postgres advisory lock on the metadata store, held while one source runs."""
    key = int.from_bytes(source_id.bytes[:8], "big", signed=True)
    with engine.connect() as conn:
        got = bool(conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}))
        conn.commit()
        try:
            yield got
        finally:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()
