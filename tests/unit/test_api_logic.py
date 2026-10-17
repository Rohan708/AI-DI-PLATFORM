"""Stage 4.2-4.3 without a database: every endpoint except login needs a token, the list of
endpoints is fixed, and passwords are hashed and checked properly."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from lumida.accounts.passwords import WeakPasswordError, hash_password, verify_password
from lumida.accounts.store import AccountError, Principal, normalize_email
from lumida.api.app import create_app
from lumida.graph.models import Role


@pytest.fixture
def client() -> TestClient:
    # Never connects: a request without a token, or with one that isn't ours, is refused
    # before any database work.
    return TestClient(create_app(create_engine("postgresql+psycopg://nobody@127.0.0.1:1/none")))


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/sources"),
        ("get", "/api/v1/sources/shop/findings"),
        ("get", "/api/v1/findings/00000000-0000-0000-0000-000000000000"),
        ("post", "/api/v1/rules/00000000-0000-0000-0000-000000000000/review"),
        ("post", "/api/v1/sources/shop/answers"),
        ("get", "/api/v1/users"),
        ("get", "/api/v1/audit"),
        ("get", "/api/v1/auth/me"),
    ],
)
def test_every_endpoint_needs_a_login(client: TestClient, method: str, path: str) -> None:
    missing = client.request(method, path)
    wrong = client.request(method, path, headers={"Authorization": "Bearer nope"})
    assert (missing.status_code, wrong.status_code) == (401, 401)
    assert missing.json() == {"detail": "log in first (missing, expired or unknown token)"}


def test_the_endpoints(client: TestClient) -> None:
    schema = client.get("/api/openapi.json").json()  # the description itself is public
    routes = {(m.upper(), p) for p, ops in schema["paths"].items() for m in ops}
    assert routes == {
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/logout"),
        ("GET", "/api/v1/auth/me"),
        ("POST", "/api/v1/auth/password"),
        ("GET", "/api/v1/auth/keys"),
        ("POST", "/api/v1/auth/keys"),
        ("DELETE", "/api/v1/auth/keys/{key_id}"),
        ("GET", "/api/v1/users"),
        ("POST", "/api/v1/users"),
        ("PATCH", "/api/v1/users/{user_id}"),
        ("POST", "/api/v1/users/{user_id}/password"),
        ("GET", "/api/v1/audit"),
        ("GET", "/api/v1/sources"),
        ("GET", "/api/v1/sources/{name}"),
        ("GET", "/api/v1/sources/{name}/runs"),
        ("GET", "/api/v1/sources/{name}/health"),
        ("GET", "/api/v1/sources/{name}/findings"),
        ("GET", "/api/v1/findings/{finding_id}"),
        ("POST", "/api/v1/findings/{finding_id}/review"),
        ("GET", "/api/v1/sources/{name}/rules"),
        ("POST", "/api/v1/sources/{name}/rules"),
        ("GET", "/api/v1/rules/{rule_id}"),
        ("POST", "/api/v1/rules/{rule_id}/review"),
        ("GET", "/api/v1/sources/{name}/relationships"),
        ("POST", "/api/v1/relationships/{relationship_id}/review"),
        ("GET", "/api/v1/sources/{name}/questions"),
        ("POST", "/api/v1/sources/{name}/answers"),
    }
    assert client.get("/api/docs").status_code == 200


def test_passwords() -> None:
    stored = hash_password("correct horse battery")
    assert stored.startswith("scrypt$") and "correct" not in stored  # noqa: PT018
    assert verify_password("correct horse battery", stored)
    assert not verify_password("correct horse batterY", stored)
    assert not verify_password("anything", "not-a-hash")
    assert hash_password("correct horse battery") != stored  # a new salt every time
    with pytest.raises(WeakPasswordError, match="12 characters"):
        hash_password("short")


def test_roles_and_emails() -> None:
    def who(role: Role) -> Principal:
        return Principal(uuid.uuid4(), uuid.uuid4(), "a@b.c", role, uuid.uuid4())

    assert who(Role.ADMIN).can(Role.REVIEWER)
    assert who(Role.REVIEWER).can(Role.VIEWER)
    assert not who(Role.REVIEWER).can(Role.ADMIN)
    assert not who(Role.VIEWER).can(Role.REVIEWER)
    assert normalize_email("  Maria@Shop.COM ") == "maria@shop.com"
    with pytest.raises(AccountError):
        normalize_email("maria")
