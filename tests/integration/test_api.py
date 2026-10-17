"""Stage 4.2 against real Postgres: one nightly run on the test lab, then everything a
person does in the web app, through the API, logged in as a reviewer: sources, health,
findings (filter, page, review with an audit trail), rules (add, review), relationships,
questions. Accounts themselves: test_accounts.py."""

import json
import socket
import time
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from lumida.accounts.store import create_user
from lumida.alerting.notifier import AlertMessage
from lumida.api.app import api_server, create_app, start_api_thread
from lumida.db import create_db_engine
from lumida.graph.models import DataSource, Role, SourceKind
from lumida.ingestion.postgres import PostgresAdapter
from lumida.ingestion.settings import ScanSettings
from lumida.ingestion.sources import add_source
from lumida.lab.runner import build_lab
from lumida.pipeline import run_pipeline
from lumida.service.migrate import upgrade_to_head

PASSWORD = "a long test password"
MARIA = "maria@shop.example"


class Inbox:
    """Collects alerts instead of calling Slack (no external calls in tests)."""

    def __init__(self) -> None:
        self.messages: list[AlertMessage] = []

    def send(self, message: AlertMessage) -> None:
        self.messages.append(message)


@pytest.fixture(scope="module")
def store(make_database: Callable[[], str]) -> Iterator[Engine]:
    """A metadata store with the lab registered as ``shop`` and run once."""
    store_url, lab_url = make_database(), make_database()
    upgrade_to_head(store_url)
    lab = create_db_engine(lab_url)
    build_lab(lab, seed=7, size="tiny")
    lab.dispose()
    engine = create_db_engine(store_url)
    with Session(engine) as session:
        source = add_source(
            session, name="shop", kind=SourceKind.POSTGRES, connection_ref="SHOP_DB_URL",
            settings={"exclude_schemas": ["aide_lab"], "timezone": "Europe/Berlin"},
        )  # fmt: skip

        def factory(_s: DataSource) -> Any:
            return PostgresAdapter(lab_url, ScanSettings(exclude_schemas=("aide_lab",)))

        result = run_pipeline(session, source, notifier=Inbox(), adapter_factory=factory)
        assert result.ok, result.summary()
        create_user(session, tenant_id=source.tenant_id, email=MARIA, name="Maria",
                    role=Role.REVIEWER, password=PASSWORD, by="test")  # fmt: skip
        session.commit()
    yield engine
    engine.dispose()


def logged_in(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, str]:
    reply = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert reply.status_code == 200, reply.text
    return {"Authorization": f"Bearer {reply.json()['token']}"}


@pytest.fixture(scope="module")
def api(store: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(store)) as client:
        client.headers.update(logged_in(client, MARIA))
        yield client


def _ok(response: Any, code: int = 200) -> Any:
    assert response.status_code == code, response.text
    return response.json()


def test_sources_runs_and_health(api: TestClient) -> None:
    (shop,) = _ok(api.get("/api/v1/sources"))
    assert (shop["name"], shop["connection_ref"]) == ("shop", "SHOP_DB_URL")
    assert (shop["run_at"], shop["timezone"]) == ("03:00", "Europe/Berlin")
    assert (shop["baseline_done"], shop["last_run"]["status"]) == (True, "succeeded")
    assert shop["health_score"] is not None
    assert sum(shop["open_findings"].values()) > 0
    assert _ok(api.get("/api/v1/sources/shop")) == shop
    assert "postgresql" not in api.get("/api/v1/sources").text  # never a connection URL

    (run,) = _ok(api.get("/api/v1/sources/shop/runs"))
    assert run["id"] == shop["last_run"]["id"]

    health = _ok(api.get("/api/v1/sources/shop/health"))
    assert health["source"]["score"] == shop["health_score"]
    assert health["previous_score"] is None  # first run
    scores = [t["score"] for t in health["tables"]]
    assert scores == sorted(scores)
    assert health["tables"][0]["table"].count(".") == 1  # schema.table

    assert _ok(api.get("/api/v1/sources/nope"), 404) == {
        "detail": "no data source named 'nope'; add it with `lumida source add`"
    }


def test_findings_list_filter_and_page(api: TestClient) -> None:
    page = _ok(api.get("/api/v1/sources/shop/findings"))
    assert page["total"] == len(page["items"]) > 1
    order = ["critical", "high", "medium", "low", "info"]
    ranks = [order.index(f["severity"]) for f in page["items"]]
    assert ranks == sorted(ranks)  # most severe first

    first = page["items"][0]
    only = _ok(api.get("/api/v1/sources/shop/findings", params={"severity": first["severity"]}))
    assert {f["severity"] for f in only["items"]} == {first["severity"]}
    one = _ok(api.get("/api/v1/sources/shop/findings", params={"limit": 1, "offset": 1}))
    assert (one["total"], one["items"]) == (page["total"], page["items"][1:2])
    assert (
        _ok(api.get("/api/v1/sources/shop/findings", params={"status": "rejected"}))["total"] == 0
    )
    _ok(api.get("/api/v1/sources/shop/findings", params={"severity": "awful"}), 422)


def test_reviewing_a_finding_keeps_an_audit_trail(api: TestClient) -> None:
    item = _ok(api.get("/api/v1/sources/shop/findings"))["items"][-1]
    detail = _ok(api.get(f"/api/v1/findings/{item['id']}"))
    assert all(detail[k] for k in ("description", "evidence", "history"))

    body = {"action": "confirm", "note": "known since the migration"}
    after = _ok(api.post(f"/api/v1/findings/{item['id']}/review", json=body))
    assert (after["status"], after["reviewed_by"]) == ("confirmed", MARIA)  # from the login
    last = after["history"][-1]
    assert (last["by"], last["from_status"], last["to_status"], last["note"]) == (
        MARIA, "open", "confirmed", "known since the migration",
    )  # fmt: skip

    reopen = {"action": "reopen"}  # confirmed -> open isn't allowed
    assert "can't become open" in _ok(api.post(f"/api/v1/findings/{item['id']}/review",
                                               json=reopen), 409)["detail"]  # fmt: skip
    _ok(api.post(f"/api/v1/findings/{item['id']}/review", json={"action": "delete"}), 422)
    _ok(api.get("/api/v1/findings/00000000-0000-0000-0000-000000000000"), 404)


def test_rules_added_and_reviewed(api: TestClient) -> None:
    relationship = _ok(api.get("/api/v1/sources/shop/relationships"))[0]
    table, column = relationship["from_table"], relationship["from_columns"][0]
    spec = {"kind": "not_null", "table": table, "column": column}
    rule = _ok(api.post("/api/v1/sources/shop/rules", json={"spec": spec}), 201)
    assert (rule["status"], rule["origin"], rule["created_by"]) == ("active", "user", MARIA)
    assert _ok(api.get(f"/api/v1/rules/{rule['id']}")) == rule
    assert rule["id"] in {r["id"] for r in _ok(api.get("/api/v1/sources/shop/rules"))}

    again = _ok(api.post("/api/v1/sources/shop/rules", json={"spec": spec}), 409)
    assert "already exists" in again["detail"]
    bad = {"spec": {"kind": "not_null", "table": table}}
    assert "column" in _ok(api.post("/api/v1/sources/shop/rules", json=bad), 422)["detail"]

    off = _ok(api.post(f"/api/v1/rules/{rule['id']}/review",
                       json={"action": "disable", "note": "too noisy"}))  # fmt: skip
    assert (off["status"], off["history"][-1]["by"]) == ("disabled", MARIA)
    assert (
        _ok(api.get("/api/v1/sources/shop/rules", params={"status": "disabled"}))[0]["id"]
        == rule["id"]
    )


def test_relationships_reviewed(api: TestClient) -> None:
    found = _ok(api.get("/api/v1/sources/shop/relationships"))
    inferred = next(r for r in found if r["kind"] == "inferred")
    assert inferred["confidence"] is not None
    assert inferred["signals"]
    body = {"action": "reject"}
    after = _ok(api.post(f"/api/v1/relationships/{inferred['id']}/review", json=body))
    assert (after["status"], after["reviewed_by"]) == ("rejected", MARIA)
    shown = {r["id"] for r in _ok(api.get("/api/v1/sources/shop/relationships"))}
    assert inferred["id"] not in shown
    everything = api.get("/api/v1/sources/shop/relationships", params={"include_rejected": True})
    assert inferred["id"] in {r["id"] for r in _ok(everything)}


def test_questions_answered(api: TestClient) -> None:
    asked = _ok(api.get("/api/v1/sources/shop/questions", params={"limit": 50}))
    closed = next(q for q in asked if q["key"] == "closed_weekdays")
    assert closed["suggested"] == "none"
    body = {"key": "closed_weekdays", "value": "sun"}
    assert "sun" in _ok(api.post("/api/v1/sources/shop/answers", json=body))["outcome"].lower()
    keys = {q["key"] for q in _ok(api.get("/api/v1/sources/shop/questions", params={"limit": 50}))}
    assert "closed_weekdays" not in keys
    assert _ok(api.post("/api/v1/sources/shop/answers", json=body), 409)["detail"].startswith(
        "no open question"
    )
    unclear = {"key": "closed_weekdays", "value": "?"}  # answered: not open now
    _ok(api.post("/api/v1/sources/shop/answers", json=unclear), 409)


def test_the_api_runs_as_a_server_next_to_the_scheduler(store: Engine, api: TestClient) -> None:
    with socket.socket() as s:  # a free port
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = api_server(create_app(store), "127.0.0.1", port)
    thread = start_api_thread(server)
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        auth = {"Authorization": api.headers["Authorization"]}  # the same login works
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/sources", headers=auth)
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 (http only)
            assert [s["name"] for s in json.loads(response.read())] == ["shop"]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert not thread.is_alive()
