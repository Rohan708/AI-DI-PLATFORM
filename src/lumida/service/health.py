"""A tiny health endpoint for Docker and monitoring tools: ``GET /healthz``.

Answers 200 while the scheduler loop is alive (it ticked recently) and 503 when it has
stalled, with what each source last did and when it runs next. It shows names, times and
step summaries only; never data, never connection details. It binds to 127.0.0.1 by
default (Docker's health check runs inside the container); set ``LUMIDA_HEALTH_HOST`` to
reach it from outside.
"""

import json
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from lumida.graph.models import utcnow
from lumida.service.runner import ServiceState

# The loop counts as stalled when it hasn't ticked for this many tick intervals.
STALLED_AFTER_TICKS = 3


def health_report(
    state: ServiceState, tick_seconds: float, now: datetime
) -> tuple[int, dict[str, Any]]:
    stalled = state.heartbeat is None or now - state.heartbeat > timedelta(
        seconds=STALLED_AFTER_TICKS * tick_seconds
    )
    failing = [s.name for s in state.sources.values() if s.last_ok is False]
    body = {
        "status": "stalled" if stalled else ("degraded" if failing else "ok"),
        "started_at": _iso(state.started_at),
        "heartbeat": _iso(state.heartbeat),
        "failing_sources": failing,
        "sources": [
            {
                "name": s.name,
                "schedule": s.schedule,
                "last_run_at": _iso(s.last_run_at),
                "last_ok": s.last_ok,
                "next_run_at": _iso(s.next_run_at),
            }
            for s in sorted(state.sources.values(), key=lambda s: s.name)
        ],
    }
    # A failing source is the customer database's problem (and alerted), not the
    # container's: only a stalled loop makes the service itself unhealthy.
    return (503 if stalled else 200), body


def start_health_server(
    state: ServiceState, host: str, port: int, tick_seconds: float
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?")[0] not in ("/healthz", "/"):
                self.send_error(404)
                return
            code, body = health_report(state, tick_seconds, utcnow())
            payload = json.dumps(body, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return  # health checks every few seconds would drown the real log

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    return server


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None
