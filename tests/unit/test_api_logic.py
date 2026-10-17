"""Stage 4.2 without a database: the API is off without a token, every endpoint needs the
token, and the endpoint list is what the web app will be built on."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from lumida.api.app import create_app
from lumida.config import Settings

TOKEN = "test-token-not-a-secret"


@pytest.fixture
def client() -> TestClient:
    # Never connects: the token is checked before any database work.
    engine = create_engine("postgresql+psycopg://nobody@127.0.0.1:1/none")
    return TestClient(create_app(engine, TOKEN))


def test_the_api_is_off_without_a_token() -> None:
    assert Settings.model_validate({"database_url": "postgresql://x"}).api_token is None
    blank = Settings.model_validate({"database_url": "postgresql://x", "api_token": " "})
    assert blank.api_token is None  # `LUMIDA_API_TOKEN=` in .env
    with pytest.raises(ValueError, match="token"):
        create_app(create_engine("postgresql+psycopg://nobody@127.0.0.1:1/none"), "")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/sources"),
        ("get", "/api/v1/sources/shop/findings"),
        ("get", "/api/v1/findings/00000000-0000-0000-0000-000000000000"),
        ("post", "/api/v1/rules/00000000-0000-0000-0000-000000000000/review"),
        ("post", "/api/v1/sources/shop/answers"),
    ],
)
def test_every_endpoint_needs_the_token(client: TestClient, method: str, path: str) -> None:
    missing = client.request(method, path)
    wrong = client.request(method, path, headers={"Authorization": "Bearer nope"})
    assert (missing.status_code, wrong.status_code) == (401, 401)
    assert missing.json() == {"detail": "missing or wrong API token"}


def test_the_endpoints(client: TestClient) -> None:
    schema = client.get("/api/openapi.json").json()  # the description itself is public
    routes = {(m.upper(), p) for p, ops in schema["paths"].items() for m in ops}
    assert routes == {
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
