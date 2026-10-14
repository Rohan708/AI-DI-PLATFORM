"""``lumida serve``: the long-running service in the self-hosted container.

On start: bring the metadata store up to date (migrations), start the health endpoint,
then tick every ``LUMIDA_SCHEDULER_TICK_SECONDS`` and run whatever is due. Stops cleanly
on Ctrl+C / ``docker stop`` (SIGTERM): the run in progress finishes its current source
first, so give the container a generous stop timeout.
"""

import json
import logging
import signal
import sys
import threading
from datetime import datetime
from typing import Any

from lumida.config import Settings
from lumida.db import create_db_engine
from lumida.graph.models import utcnow
from lumida.service.health import start_health_server
from lumida.service.migrate import upgrade_to_head
from lumida.service.runner import ServiceState, run_due

log = logging.getLogger("lumida.service")


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log collectors. Messages never contain row values."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created).astimezone().isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level)
    logging.getLogger("alembic").setLevel(logging.WARNING)


def serve(
    settings: Settings,
    *,
    once: bool = False,
    migrate: bool = True,
    stop: threading.Event | None = None,
) -> ServiceState:
    stop = stop or threading.Event()
    url = settings.database_url.get_secret_value()
    if migrate:
        revision = upgrade_to_head(url)
        log.info("metadata store at migration %s", revision)
    state = ServiceState(started_at=utcnow())
    tick = settings.scheduler_tick_seconds
    server = None
    if settings.health_port and not once:
        server = start_health_server(state, settings.health_host, settings.health_port, tick)
        log.info(
            "health endpoint on http://%s:%s/healthz", settings.health_host, settings.health_port
        )
    engine = create_db_engine(url)
    log.info("scheduler started (checks every %gs)", tick)
    try:
        while not stop.is_set():
            try:
                for report in run_due(engine, state):
                    if report.skipped:
                        continue
                    log.info("%s", report.summary.replace("\n", " | "))
            except Exception:  # e.g. the metadata store is briefly unreachable: try again
                log.exception("scheduler tick failed; retrying next tick")
            if once:
                break
            stop.wait(tick)
    finally:
        engine.dispose()
        if server is not None:
            server.shutdown()
        log.info("scheduler stopped")
    return state


def install_signal_handlers(stop: threading.Event) -> None:
    def handle(signum: int, _frame: object) -> None:
        log.info("stopping (signal %s); finishing the current run first", signum)
        stop.set()

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, handle)
