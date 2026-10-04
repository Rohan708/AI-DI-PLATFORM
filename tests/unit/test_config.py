import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_data_engineer.config import Settings


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run from an empty dir (no .env) with no AIDE_* vars leaking in from the shell."""
    monkeypatch.chdir(tmp_path)
    for key in [k for k in os.environ if k.startswith("AIDE_")]:
        monkeypatch.delenv(key)


def test_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDE_DATABASE_URL", "postgresql+psycopg://u:pw@localhost:5432/db")
    monkeypatch.setenv("AIDE_LOG_LEVEL", "DEBUG")

    settings = Settings()

    assert settings.database_url.get_secret_value().endswith("/db")
    assert settings.log_level == "DEBUG"
    assert settings.slack_webhook_url is None


def test_database_url_is_required() -> None:
    with pytest.raises(ValidationError):
        Settings()


def test_secrets_are_not_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDE_DATABASE_URL", "postgresql+psycopg://u:topsecret@localhost/db")
    monkeypatch.setenv("AIDE_SLACK_WEBHOOK_URL", "https://hooks.example/topsecret")

    assert "topsecret" not in repr(Settings())
