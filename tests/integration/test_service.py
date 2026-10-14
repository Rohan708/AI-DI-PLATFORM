"""Stage 4.1 against real Postgres: migrations from the packaged files, the scheduler runs
due sources once (and only once), a failing run alerts and isn't retried every minute,
the per-source lock stops double runs, and the health endpoint answers."""

import json
import urllib.request
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from lumida.alerting.notifier import AlertMessage
from lumida.config import Settings
from lumida.db import create_db_engine
from lumida.graph.models import DataSource, IngestionRun, SourceKind, Tenant, utcnow
from lumida.ingestion.postgres import PostgresAdapter
from lumida.ingestion.settings import ScanSettings
from lumida.lab.runner import build_lab
from lumida.service.health import start_health_server
from lumida.service.migrate import current_revision, upgrade_to_head
from lumida.service.runner import ServiceState, run_due, source_lock
from lumida.service.serve import serve

LAB = ScanSettings(exclude_schemas=("aide_lab",))


class Inbox:
    def __init__(self) -> None:
        self.messages: list[AlertMessage] = []

    def send(self, message: AlertMessage) -> None:
        self.messages.append(message)


@pytest.fixture
def store(make_database: Callable[[], str]) -> Iterator[tuple[str, Engine]]:
    """A fresh metadata store, upgraded with the migrations shipped in the package."""
    url = make_database()
    assert upgrade_to_head(url) == current_revision(url)
    engine = create_db_engine(url)
    yield url, engine
    engine.dispose()


def _source(
    engine: Engine, name: str, ref: str, run_at: str, *, added_days_ago: float = 2
) -> DataSource:
    with Session(engine) as session:
        tenant = Tenant(name=f"t-{name}")
        session.add(tenant)
        session.flush()
        settings = {"exclude_schemas": ["aide_lab"], "run_at": run_at}
        source = DataSource(
            tenant_id=tenant.id, name=name, kind=SourceKind.POSTGRES, connection_ref=ref,
            settings=settings, created_at=utcnow() - timedelta(days=added_days_ago),
        )  # fmt: skip
        session.add(source)
        session.commit()
        session.refresh(source)
        session.expunge(source)
        return source


def _due_now() -> str:
    """A run time an hour ago (UTC): due right away for a source added two days ago."""
    return (utcnow() - timedelta(hours=1)).strftime("%H:%M")


def _runs(engine: Engine, source: DataSource) -> int:
    with Session(engine) as session:
        query = select(func.count()).select_from(IngestionRun)
        return int(session.scalar(query.where(IngestionRun.data_source_id == source.id)) or 0)


def test_the_scheduler_runs_due_sources_once(
    store: tuple[str, Engine], make_database: Callable[[], str]
) -> None:
    _, engine = store
    lab_url = make_database()
    lab = create_db_engine(lab_url)
    build_lab(lab, seed=101, size="tiny")
    lab.dispose()
    shop = _source(engine, "shop", "UNUSED", _due_now())
    later = _source(engine, "later", "UNUSED", _due_now(), added_days_ago=0)  # just added
    state, inbox = ServiceState(), Inbox()

    def factory(_s: DataSource) -> Any:
        return PostgresAdapter(lab_url, LAB)

    first = run_due(engine, state, adapter_factory=factory, notifier_factory=lambda _s: inbox)

    assert [(r.source, r.ok) for r in first] == [("shop", True)], [r.summary for r in first]
    assert _runs(engine, shop) == 1
    assert _runs(engine, later) == 0  # added after today's run time: waits for tomorrow
    assert state.sources["later"].next_run_at is not None
    assert inbox.messages == []  # nothing failed
    again = run_due(engine, state, adapter_factory=factory, notifier_factory=lambda _s: inbox)
    assert again == []  # already ran this cycle
    assert _runs(engine, shop) == 1


def test_a_failing_run_alerts_and_waits_for_the_next_night(store: tuple[str, Engine]) -> None:
    _, engine = store
    _source(engine, "broken", "LUMIDA_NO_SUCH_DATABASE_URL", _due_now())
    state, inbox = ServiceState(), Inbox()

    first = run_due(engine, state, notifier_factory=lambda _s: inbox)
    assert [(r.source, r.ok) for r in first] == [("broken", False)]
    assert len(inbox.messages) == 1
    assert inbox.messages[0].title == "Lumida: the nightly run for broken failed"
    assert any("scan" in line for line in inbox.messages[0].lines)
    assert state.sources["broken"].last_ok is False

    assert run_due(engine, state, notifier_factory=lambda _s: inbox) == []  # not every minute
    assert len(inbox.messages) == 1


def test_a_source_already_running_elsewhere_is_skipped(store: tuple[str, Engine]) -> None:
    _, engine = store
    busy = _source(engine, "busy", "LUMIDA_NO_SUCH_DATABASE_URL", _due_now())
    state, inbox = ServiceState(), Inbox()
    with source_lock(engine, busy.id) as locked:  # another Lumida holds it
        assert locked
        (report,) = run_due(engine, state, notifier_factory=lambda _s: inbox)
    assert report.skipped == "already running elsewhere"
    assert inbox.messages == []


def test_serve_once_and_the_health_endpoint(
    store: tuple[str, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, engine = store
    _source(engine, "quiet", "UNUSED", _due_now(), added_days_ago=0)  # waits for tomorrow
    settings = Settings.model_validate({"database_url": url, "health_port": 0})

    state = serve(settings, once=True)  # migrations (already current) + one tick

    assert state.heartbeat is not None
    assert state.sources["quiet"].next_run_at is not None
    server = start_health_server(state, "127.0.0.1", 0, tick_seconds=60)
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
            body = json.loads(response.read())
            assert response.status == 200
        assert body["status"] == "ok"
        assert [s["name"] for s in body["sources"]] == ["quiet"]
        assert "UNUSED" not in json.dumps(body)  # no connection details exposed
    finally:
        server.shutdown()
