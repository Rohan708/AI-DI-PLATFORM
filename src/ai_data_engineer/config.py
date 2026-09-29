from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Database
    database_url: str

    # Snowflake
    snowflake_account: str
    snowflake_user: str
    snowflake_private_key_path: str
    snowflake_private_key_passphrase: str | None = None
    snowflake_role: str | None = None
    snowflake_warehouse: str | None = None
    snowflake_database: str | None = None

    # dbt
    dbt_manifest_path: str | None = None

    # LLM (Phase 3+)
    openai_api_key: str | None = None

    # Logging
    log_level: str = "INFO"

    # Detection Engine Thresholds
    detection_trailing_versions: int = 7
    detection_null_rate_abs_threshold: float = 0.10
    detection_null_rate_zscore_threshold: float = 3.0
    detection_distinct_drop_threshold: float = 0.15

    # Alerting
    slack_webhook_url: str | None = None
    slack_fallback_channel: str = "#data-alerts"
    alert_digest_threshold: int = 3

    # Health Score Weights
    health_score_weight_quality: int = 20
    health_score_weight_schema: int = 15
    health_score_weight_semantic: int = 10
    health_score_weight_architecture: int = 10
    health_score_weight_cost: int = 5

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
