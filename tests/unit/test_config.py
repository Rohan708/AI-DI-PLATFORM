import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from lumida.config import Settings


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run from an empty dir (no .env) with no LUMIDA_* / AIDE_* vars leaking in."""
    monkeypatch.chdir(tmp_path)
    for key in [k for k in os.environ if k.startswith(("LUMIDA_", "AIDE_"))]:
        monkeypatch.delenv(key)


def test_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUMIDA_DATABASE_URL", "postgresql+psycopg://u:pw@localhost:5432/db")
    monkeypatch.setenv("LUMIDA_LOG_LEVEL", "DEBUG")

    settings = Settings()

    assert settings.database_url.get_secret_value().endswith("/db")
    assert settings.log_level == "DEBUG"
    assert settings.slack_webhook_url is None


def test_database_url_is_required() -> None:
    with pytest.raises(ValidationError):
        Settings()


def test_secrets_are_not_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUMIDA_DATABASE_URL", "postgresql+psycopg://u:topsecret@localhost/db")
    monkeypatch.setenv("LUMIDA_SLACK_WEBHOOK_URL", "https://hooks.example/topsecret")

    assert "topsecret" not in repr(Settings())


def test_names_from_before_the_rename_still_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lines = ["AIDE_DATABASE_URL=postgresql+psycopg://u:p@old/db", "AIDE_LLM_PROVIDER=gemini"]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("AIDE_LOG_LEVEL", "WARNING")
    settings = Settings()
    assert settings.database_url.get_secret_value().endswith("@old/db")
    assert (settings.llm_provider, settings.log_level) == ("gemini", "WARNING")

    monkeypatch.setenv("LUMIDA_DATABASE_URL", "postgresql+psycopg://u:p@new/db")
    assert Settings().database_url.get_secret_value().endswith("@new/db")  # new name wins
