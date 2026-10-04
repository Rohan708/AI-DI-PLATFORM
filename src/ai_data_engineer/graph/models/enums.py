"""Enumerations stored as VARCHAR (see ``StrEnumType``). Values are the stored strings."""

from enum import StrEnum


class SourceKind(StrEnum):
    POSTGRES = "postgres"
    MYSQL = "mysql"
    SQLSERVER = "sqlserver"
    ORACLE = "oracle"
    SNOWFLAKE = "snowflake"
    BIGQUERY = "bigquery"
    DATABRICKS = "databricks"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"  # finished, but some tables/columns could not be read
    FAILED = "failed"


class AssetKind(StrEnum):
    TABLE = "table"
    VIEW = "view"
    MATERIALIZED_VIEW = "materialized_view"
    FOREIGN_TABLE = "foreign_table"
    OTHER = "other"


class TypeFamily(StrEnum):
    """Database-neutral type family, so "same concept, different types" is comparable
    across tables and across database engines. The native type is stored alongside."""

    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    TIME = "time"
    INTERVAL = "interval"
    BINARY = "binary"
    JSON = "json"
    UUID = "uuid"
    ARRAY = "array"
    OTHER = "other"


class RelationshipKind(StrEnum):
    DECLARED = "declared"  # a real FK constraint in the database
    INFERRED = "inferred"  # discovered from evidence; must carry a confidence


class RelationshipStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class Origin(StrEnum):
    """Who produced a rule or finding. AI-produced rows must carry a confidence."""

    SYSTEM = "system"
    USER = "user"
    AI = "ai"


class RuleStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    REJECTED = "rejected"
    DISABLED = "disabled"


class FindingCategory(StrEnum):
    """Mirrors the anomaly taxonomy in docs/product/vision_and_roadmap.md §3."""

    STRUCTURAL = "structural"
    RELATIONAL = "relational"
    COLUMN_VALUE = "column_value"
    BUSINESS_RULE = "business_rule"
    TIME_SERIES = "time_series"
    ROW_OUTLIER = "row_outlier"
    CROSS_SYSTEM = "cross_system"
    SEMANTIC = "semantic"
    DB_HEALTH = "db_health"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingStatus(StrEnum):
    OPEN = "open"
    CONFIRMED = "confirmed"  # a human agreed it's real; still unresolved
    REJECTED = "rejected"  # a human said it's not a problem
    RESOLVED = "resolved"


# Statuses in which a finding counts as "active" (used for dedup).
ACTIVE_FINDING_STATUSES = (FindingStatus.OPEN, FindingStatus.CONFIRMED)
