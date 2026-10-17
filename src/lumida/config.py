"""Application settings, loaded from environment variables (prefix ``LUMIDA_``) or ``.env``.

Every variable is documented in ``.env.example``. Secrets are ``SecretStr`` so they
never appear in reprs or logs; read them with ``.get_secret_value()`` only at the
point of use.

Names from before the rename to Lumida (``AIDE_DATABASE_URL`` ...) keep working: they're
read after the ``LUMIDA_`` ones, so an existing ``.env`` needs no change.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogFormat = Literal["text", "json"]
# "none" = AI features off (everything else still works). More providers plug in here.
LLMProvider = Literal["none", "gemini"]

ENV_PREFIX = "LUMIDA_"
LEGACY_ENV_PREFIX = "AIDE_"  # the product's working name until 2026-10-14


class _LegacyPrefixSource(PydanticBaseSettingsSource):
    """``AIDE_*`` variables (process environment, then ``.env``), used only for settings
    that no ``LUMIDA_*`` variable sets."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False  # values come from __call__

    def __call__(self) -> dict[str, Any]:
        env_file = self.config.get("env_file")
        from_file: dict[str, str | None] = {}
        if isinstance(env_file, str) and Path(env_file).exists():
            from_file = dotenv_values(env_file)
        values: dict[str, Any] = {}
        for name in self.settings_cls.model_fields:
            key = LEGACY_ENV_PREFIX + name.upper()
            value = os.environ.get(key) or from_file.get(key)
            if value:
                values[name] = value
        return values


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Metadata store (our Postgres). Required: no default, so a missing value fails loudly.
    database_url: SecretStr

    log_level: LogLevel = "INFO"
    log_format: LogFormat = "text"  # "json" for log collectors (lumida serve)

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

    # The self-hosted service (`lumida serve`, Stage 4.1).
    scheduler_tick_seconds: float = 60.0  # how often it checks what's due
    health_host: str = "127.0.0.1"  # 0.0.0.0 to reach /healthz from outside the container
    health_port: int = 8080  # 0 = no health endpoint

    # The API (Stage 4.2) with personal logins (4.3; create people with `lumida user add`).
    api_enabled: bool = True  # inside `lumida serve`
    api_host: str = "127.0.0.1"  # the image sets 0.0.0.0; compose publishes it on localhost only
    api_port: int = 8000
    session_hours: float = 12.0  # how long a login lasts
    login_max_failures: int = 5  # wrong passwords in a row before the account is locked
    login_lockout_minutes: float = 15.0

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Earlier sources win: arguments, LUMIDA_* (env, then .env), then legacy AIDE_*.
        legacy = _LegacyPrefixSource(settings_cls)
        return init_settings, env_settings, dotenv_settings, legacy, file_secret_settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Tests should construct ``Settings()`` directly instead."""
    return Settings()
