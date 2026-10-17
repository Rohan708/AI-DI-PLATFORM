"""Stage 4.3 against real Postgres: logging in and out, lockout after wrong passwords,
roles, personal API keys, people management, tenant isolation and the audit log."""

import io
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from lumida.accounts.store import AccountError, AuthPolicy, create_user, find_user, set_disabled
from lumida.api.app import create_app
from lumida.config import get_settings
from lumida.db import create_db_engine
from lumida.detection.recording import record_finding
from lumida.graph.models import (
    AuditEvent,
    DataSource,
    FindingCategory,
    Role,
    Severity,
    SourceKind,
    Tenant,
    User,
    utcnow,
)
from lumida.ingestion.sources import add_source, default_tenant
from lumida.service.migrate import upgrade_to_head

PASSWORD = "a long test password"
ADMIN, REVIEWER, VIEWER = "admin@shop.example", "rev@shop.example", "view@shop.example"
OUTSIDER = "boss@other.example"  # in a second tenant


@pytest.fixture(scope="module")
def store(make_database: Callable[[], str]) -> Iterator[tuple[str, Engine]]:
    """Two tenants: ``default`` (source shop, one finding, three people) and ``other``
    (source theirs, one admin)."""
    url = make_database()
    upgrade_to_head(url)
    engine = create_db_engine(url)
    with Session(engine) as session:
        shop = add_source(session, name="shop", kind=SourceKind.POSTGRES, connection_ref="X")
        record_finding(
            session, source=shop, fingerprint="f1", category=FindingCategory.STRUCTURAL,
            check_name="missing_primary_key", severity=Severity.MEDIUM, title="no key",
            description="d", evidence={}, now=utcnow(),
        )  # fmt: skip
        for email, role in ((ADMIN, Role.ADMIN), (REVIEWER, Role.REVIEWER), (VIEWER, Role.VIEWER)):
            create_user(session, tenant_id=shop.tenant_id, email=email, name="", role=role,
                        password=PASSWORD, by="test")  # fmt: skip
        other = Tenant(name="other")
        session.add(other)
        session.flush()
        session.add(DataSource(tenant_id=other.id, name="theirs", kind=SourceKind.POSTGRES))
        create_user(session, tenant_id=other.id, email=OUTSIDER, name="", role=Role.ADMIN,
                    password=PASSWORD, by="test")  # fmt: skip
        session.commit()
    yield url, engine
    engine.dispose()


@pytest.fixture
def client(store: tuple[str, Engine]) -> Iterator[TestClient]:
    with TestClient(create_app(store[1])) as c:
        yield c


def _login(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, str]:
    reply = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert reply.status_code == 200, reply.text
    return {"Authorization": f"Bearer {reply.json()['token']}"}


def _audit(engine: Engine, action: str) -> list[AuditEvent]:
    with Session(engine) as session:
        return list(session.scalars(select(AuditEvent).where(AuditEvent.action == action)))


def test_log_in_look_and_log_out(client: TestClient) -> None:
    reply = client.post("/api/v1/auth/login", json={"email": " Rev@Shop.Example ", "password":
                                                    PASSWORD})  # fmt: skip
    assert reply.status_code == 200
    body = reply.json()
    assert body["token"].startswith("lmd_")
    assert (body["user"]["email"], body["user"]["role"]) == (REVIEWER, "reviewer")
    auth = {"Authorization": f"Bearer {body['token']}"}
    assert client.get("/api/v1/auth/me", headers=auth).json()["email"] == REVIEWER
    assert client.post("/api/v1/auth/logout", headers=auth).status_code == 204
    assert client.get("/api/v1/auth/me", headers=auth).status_code == 401  # that token is over


def test_wrong_passwords_lock_the_account(client: TestClient, store: tuple[str, Engine]) -> None:
    _, engine = store
    target = "locked@shop.example"
    with Session(engine) as session:
        create_user(session, tenant_id=default_tenant(session).id, email=target, name="",
                    role=Role.VIEWER, password=PASSWORD, by="test")  # fmt: skip
        session.commit()
    wrong = {"email": target, "password": "not the password"}
    unknown = client.post("/api/v1/auth/login", json={"email": "nobody@x.y", "password": "p"})
    first = client.post("/api/v1/auth/login", json=wrong)
    assert unknown.status_code == first.status_code == 401
    assert unknown.json() == first.json()  # never says whether the email exists
    for _ in range(4):
        client.post("/api/v1/auth/login", json=wrong)
    right = client.post("/api/v1/auth/login", json={"email": target, "password": PASSWORD})
    assert right.status_code == 401  # locked, even with the right password
    reasons = [e.details["reason"] for e in _audit(engine, "auth.login_failed")
               if e.actor == target]  # fmt: skip
    assert reasons[-2].endswith("locked for 15 minutes")
    assert reasons[-1] == "locked"

    admin = _login(client, ADMIN)
    user_id = next(u["id"] for u in client.get("/api/v1/users", headers=admin).json()
                   if u["email"] == target)  # fmt: skip
    reset = client.post(f"/api/v1/users/{user_id}/password", headers=admin,
                        json={"password": "a brand new password"})  # fmt: skip
    assert reset.status_code == 204
    _login(client, target, "a brand new password")  # unlocked


def test_roles(client: TestClient) -> None:
    viewer, reviewer, admin = (_login(client, e) for e in (VIEWER, REVIEWER, ADMIN))
    (finding,) = client.get("/api/v1/sources/shop/findings", headers=viewer).json()["items"]
    review = f"/api/v1/findings/{finding['id']}/review"

    refused = client.post(review, headers=viewer, json={"action": "confirm"})
    assert refused.status_code == 403
    assert refused.json()["detail"] == "this needs the reviewer role (you are viewer)"
    assert client.post(review, headers=reviewer, json={"action": "confirm"}).status_code == 200
    assert client.get("/api/v1/users", headers=reviewer).status_code == 403
    assert client.get("/api/v1/audit", headers=reviewer).status_code == 403
    assert client.get("/api/v1/users", headers=admin).status_code == 200


def test_api_keys(client: TestClient) -> None:
    me = _login(client, REVIEWER)
    made = client.post("/api/v1/auth/keys", headers=me, json={"label": "jira sync"})
    assert made.status_code == 201
    key = made.json()
    script = {"Authorization": f"Bearer {key['key']}"}
    assert client.get("/api/v1/auth/me", headers=script).json()["email"] == REVIEWER
    listed = client.get("/api/v1/auth/keys", headers=me).json()
    assert [k["label"] for k in listed] == ["jira sync"]
    assert "key" not in listed[0]  # shown only once, when made
    assert client.delete(f"/api/v1/auth/keys/{key['id']}", headers=me).status_code == 204
    assert client.get("/api/v1/auth/me", headers=script).status_code == 401


def test_changing_your_password(client: TestClient) -> None:
    email = VIEWER
    me = _login(client, email)
    key = client.post("/api/v1/auth/keys", headers=me, json={"label": "k"}).json()["key"]
    wrong = {"current_password": "nope", "new_password": "another long password"}
    assert client.post("/api/v1/auth/password", headers=me, json=wrong).status_code == 409
    short = {"current_password": PASSWORD, "new_password": "short"}
    assert client.post("/api/v1/auth/password", headers=me, json=short).status_code == 422
    change = {"current_password": PASSWORD, "new_password": "another long password"}
    assert client.post("/api/v1/auth/password", headers=me, json=change).status_code == 204
    assert client.get("/api/v1/auth/me", headers=me).status_code == 401  # sessions ended
    keyed = {"Authorization": f"Bearer {key}"}
    assert client.get("/api/v1/auth/me", headers=keyed).status_code == 200  # keys keep working
    _login(client, email, "another long password")
    back = {"current_password": "another long password", "new_password": PASSWORD}
    assert client.post("/api/v1/auth/password", headers=_login(client, email,
                       "another long password"), json=back).status_code == 204  # fmt: skip


def test_admins_manage_people(client: TestClient, store: tuple[str, Engine]) -> None:
    admin = _login(client, ADMIN)
    new = {"email": "New@Shop.Example", "name": "Nia", "role": "viewer", "password": PASSWORD}
    made = client.post("/api/v1/users", headers=admin, json=new)
    assert made.status_code == 201
    assert (made.json()["email"], made.json()["created_by"]) == ("new@shop.example", ADMIN)
    assert client.post("/api/v1/users", headers=admin, json=new).status_code == 409
    weak = {**new, "email": "weak@shop.example", "password": "short"}
    assert client.post("/api/v1/users", headers=admin, json=weak).status_code == 422

    nia = _login(client, "new@shop.example")
    path = f"/api/v1/users/{made.json()['id']}"
    promoted = client.patch(path, headers=admin, json={"role": "reviewer"})
    assert promoted.json()["role"] == "reviewer"
    assert client.patch(path, headers=admin, json={"disabled": True}).json()["disabled"]
    assert client.get("/api/v1/auth/me", headers=nia).status_code == 401  # signed out at once
    login = {"email": "new@shop.example", "password": PASSWORD}
    assert client.post("/api/v1/auth/login", json=login).status_code == 401

    me = client.get("/api/v1/auth/me", headers=admin).json()
    myself = client.patch(f"/api/v1/users/{me['id']}", headers=admin, json={"role": "viewer"})
    assert myself.status_code == 409
    # The last admin can't be disabled, even from the CLI.
    with Session(store[1]) as session, pytest.raises(AccountError, match="last admin"):
        set_disabled(session, find_user(session, ADMIN), True, by="cli:test")


def test_tenants_see_only_their_own(client: TestClient) -> None:
    ours, theirs = _login(client, ADMIN), _login(client, OUTSIDER)
    assert [s["name"] for s in client.get("/api/v1/sources", headers=theirs).json()] == ["theirs"]
    assert client.get("/api/v1/sources/shop", headers=theirs).status_code == 404
    (finding,) = client.get("/api/v1/sources/shop/findings", headers=ours).json()["items"]
    assert client.get(f"/api/v1/findings/{finding['id']}", headers=theirs).status_code == 404
    review = {"action": "reject"}
    refused = client.post(f"/api/v1/findings/{finding['id']}/review", headers=theirs, json=review)
    assert refused.status_code == 404
    assert [u["email"] for u in client.get("/api/v1/users", headers=theirs).json()] == [OUTSIDER]
    their_log = client.get("/api/v1/audit", headers=theirs).json()
    assert {e["actor"] for e in their_log} <= {OUTSIDER, "test"}


def test_the_audit_log(client: TestClient) -> None:
    admin = _login(client, ADMIN)
    reviewer = _login(client, REVIEWER)
    (finding,) = client.get("/api/v1/sources/shop/findings", headers=reviewer).json()["items"]
    client.post(f"/api/v1/findings/{finding['id']}/review", headers=reviewer,
                json={"action": "resolve", "note": "fixed upstream"})  # fmt: skip
    log = client.get("/api/v1/audit", headers=admin, params={"action": "finding.review"}).json()
    newest = log[0]
    assert (newest["actor"], newest["target_id"]) == (REVIEWER, finding["id"])
    assert newest["details"]["to"] == "resolved"
    logins = client.get("/api/v1/audit", headers=admin, params={"action": "auth"}).json()
    assert {e["action"] for e in logins} >= {"auth.login", "auth.login_failed"}
    assert PASSWORD not in client.get("/api/v1/audit", headers=admin,
                                      params={"limit": 500}).text  # fmt: skip


def test_sessions_expire(store: tuple[str, Engine]) -> None:
    with TestClient(create_app(store[1], AuthPolicy(session_hours=0))) as c:
        auth = _login(c, REVIEWER)
        assert c.get("/api/v1/auth/me", headers=auth).status_code == 401


def test_the_first_admin_from_the_command_line(
    store: tuple[str, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    from lumida.cli import main

    url, engine = store
    monkeypatch.setenv("LUMIDA_DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
        assert main(["user", "add", "first@shop.example", "--role", "admin",
                     "--password-stdin"]) == 0  # fmt: skip
        assert main(["user", "role", "first@shop.example", "reviewer"]) == 0
        monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
        assert main(["user", "add", "weak@shop.example", "--password-stdin"]) == 1
    finally:
        get_settings.cache_clear()
    with Session(engine) as session:
        user = session.scalars(select(User).where(User.email == "first@shop.example")).one()
        assert (user.role, user.created_by.startswith("cli:")) == (Role.REVIEWER, True)
        assert session.scalar(select(User).where(User.email == "weak@shop.example")) is None
