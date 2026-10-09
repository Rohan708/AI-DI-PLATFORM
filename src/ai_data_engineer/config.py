"""Application settings, loaded from environment variables (prefix ``AIDE_``) or ``.env``.

Every variable is documented in ``.env.example``. Secrets are ``SecretStr`` so they
never appear in reprs or logs; read them with ``.get_secret_value()`` only at the
point of use.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
# "none" = AI features off (everything else still works). More providers plug in here.
LLMProvider = Literal["none", "gemini"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIDE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Metadata store (our Postgres). Required: no default, so a missing value fails loudly.
    database_url: SecretStr

    log_level: LogLevel = "INFO"

    # Connections to customer databases are configured per data source (Stage 1.3),
    # not as global settings.

    # The messy test lab's own database (Stage 1.2). Never a customer database.
    lab_database_url: SecretStr | None = None

    # Alerting
    slack_webhook_url: SecretStr | None = None

    # AI-assisted discovery (Stage 2). Only proposals; checks never call the LLM.
    llm_provider: LLMProvider = "none"
    llm_model: str | None = None  # None = the provider's default (see reasoning/llm.py)
    llm_timeout_seconds: float = 120.0
    gemini_api_key: SecretStr | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Tests should construct ``Settings()`` directly instead."""
    return Settings()
