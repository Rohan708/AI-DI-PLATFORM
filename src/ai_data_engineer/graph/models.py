"""
Metadata Graph — core schema.

Design principles:
- Nodes and edges are plain Postgres tables (no graph DB needed at this scale).
- Every node is time-versioned (valid_from/valid_to) so schema drift and
  history are queryable for free — this is what root-cause investigation
  and schema-drift detection both read from.
- Edges reference nodes by stable internal id, not by name — names change
  (renames), ids don't.
- source_system + source_id together are the natural key from the
  customer's actual warehouse (e.g. Snowflake database.schema.table),
  kept separately from our internal id so re-ingestion is idempotent.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column as SAColumn,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    __allow_unmapped__ = True
    pass


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Shared versioning mixin
# ---------------------------------------------------------------------------

class Versioned:
    """
    Every node uses this pattern instead of UPDATE-in-place:
    a new row is inserted with a new valid_from, and the previous row's
    valid_to is closed. This gives full history with no separate audit table.
    valid_to = NULL means "currently active".
    """
    valid_from = SAColumn(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    valid_to = SAColumn(DateTime(timezone=True), nullable=True)
    ingested_at = SAColumn(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class SourceSystem(str, enum.Enum):
    SNOWFLAKE = "snowflake"
    BIGQUERY = "bigquery"
    DATABRICKS = "databricks"
    DBT = "dbt"
    AIRFLOW = "airflow"
    LOOKER = "looker"
    TABLEAU = "tableau"


# ---------------------------------------------------------------------------
# Node: Asset (a table or view in the warehouse)
# ---------------------------------------------------------------------------

class Asset(Versioned, Base):
    """
    A physical or logical dataset: a table, view, or materialized dbt model.
    """
    __tablename__ = "asset"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)

    # Natural key from the source system — used to match re-ingested rows
    # to the same logical asset across versions.
    source_system = SAColumn(Enum(SourceSystem), nullable=False)
    database_name = SAColumn(String, nullable=False)
    schema_name = SAColumn(String, nullable=False)
    table_name = SAColumn(String, nullable=False)

    asset_type = SAColumn(String, nullable=False)  # "table" | "view" | "materialized_view"
    row_count_estimate = SAColumn(Numeric, nullable=True)
    size_bytes_estimate = SAColumn(Numeric, nullable=True)

    # Free-form warehouse-specific metadata (clustering keys, partitioning,
    # retention settings) — kept as JSONB rather than a rigid column set
    # because this varies a lot by warehouse.
    physical_properties = SAColumn(JSONB, nullable=True)

    is_deleted = SAColumn(Boolean, nullable=False, default=False)

    columns = relationship("AssetColumn", back_populates="asset")

    __table_args__ = (
        Index(
            "ix_asset_natural_key_active",
            "source_system", "database_name", "schema_name", "table_name",
            unique=False,  # not unique because history keeps old rows
        ),
    )


# ---------------------------------------------------------------------------
# Node: AssetColumn
# ---------------------------------------------------------------------------

class AssetColumn(Versioned, Base):
    """
    A column within an Asset. Carries the statistical profile snapshot
    (null rate, distinct count, etc.) used by the deterministic detection
    layer — recomputed on each profiling run, versioned like everything else.
    """
    __tablename__ = "asset_column"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    asset_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset.id"), nullable=False)

    column_name = SAColumn(String, nullable=False)
    data_type = SAColumn(String, nullable=False)
    ordinal_position = SAColumn(Numeric, nullable=True)
    is_nullable = SAColumn(Boolean, nullable=True)

    # Statistical profile snapshot — this is what quality checks diff
    # against the previous version's row to detect drift/anomalies.
    null_rate = SAColumn(Numeric, nullable=True)
    distinct_count = SAColumn(Numeric, nullable=True)
    min_value_repr = SAColumn(String, nullable=True)   # stored as text repr, not raw value
    max_value_repr = SAColumn(String, nullable=True)

    is_pii_flagged = SAColumn(Boolean, nullable=False, default=False)
    pii_confidence = SAColumn(Numeric, nullable=True)  # 0-1, from PII detector

    asset = relationship("Asset", back_populates="columns")

    __table_args__ = (
        Index("ix_asset_column_asset_id", "asset_id"),
    )


# ---------------------------------------------------------------------------
# Node: Job (a dbt model run, Airflow task, or any transformation execution)
# ---------------------------------------------------------------------------

class JobStatus(str, enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    RUNNING = "running"
    SKIPPED = "skipped"


class Job(Base):
    """
    A single execution of a transformation (a dbt model run, an Airflow
    task instance, a Spark job). NOT versioned like other nodes — each
    execution is its own row; this table IS the run-history log that
    root-cause investigation queries.
    """
    __tablename__ = "job"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)

    source_system = SAColumn(Enum(SourceSystem), nullable=False)
    job_name = SAColumn(String, nullable=False)  # e.g. dbt model name, DAG task id

    started_at = SAColumn(DateTime(timezone=True), nullable=False)
    finished_at = SAColumn(DateTime(timezone=True), nullable=True)
    status = SAColumn(Enum(JobStatus), nullable=False)

    rows_affected = SAColumn(Numeric, nullable=True)
    bytes_scanned = SAColumn(Numeric, nullable=True)
    cost_estimate = SAColumn(Numeric, nullable=True)  # for cost-optimization features

    # The actual SQL or compiled dbt code for this run — needed by both
    # the root-cause agent (diffing against prior runs) and fix generation
    # (as context for drafting a patch).
    executed_sql = SAColumn(Text, nullable=True)
    error_message = SAColumn(Text, nullable=True)

    __table_args__ = (
        Index("ix_job_name_started", "job_name", "started_at"),
    )


# ---------------------------------------------------------------------------
# Node: Dashboard
# ---------------------------------------------------------------------------

class Dashboard(Base):
    __tablename__ = "dashboard"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    source_system = SAColumn(Enum(SourceSystem), nullable=False)
    dashboard_name = SAColumn(String, nullable=False)
    url = SAColumn(String, nullable=True)


# ---------------------------------------------------------------------------
# Node: Owner (person or team responsible for an asset/job)
# ---------------------------------------------------------------------------

class Owner(Base):
    __tablename__ = "owner"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    name = SAColumn(String, nullable=False)
    email = SAColumn(String, nullable=True)
    slack_channel = SAColumn(String, nullable=True)
    team = SAColumn(String, nullable=True)


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------
# Edges are their own tables rather than foreign keys embedded in nodes,
# because several edge types are many-to-many and some (DERIVES_FROM)
# need their own attributes (e.g. the transformation expression).

class ProducesEdge(Base):
    """Job -> Asset : this job writes/creates this asset."""
    __tablename__ = "edge_produces"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    job_id = SAColumn(UUID(as_uuid=True), ForeignKey("job.id"), nullable=False)
    asset_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset.id"), nullable=False)

    __table_args__ = (UniqueConstraint("job_id", "asset_id"),)


class ReadsFromEdge(Base):
    """Job -> Asset : this job reads this asset as input."""
    __tablename__ = "edge_reads_from"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    job_id = SAColumn(UUID(as_uuid=True), ForeignKey("job.id"), nullable=False)
    asset_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset.id"), nullable=False)

    __table_args__ = (UniqueConstraint("job_id", "asset_id"),)


class DerivesFromEdge(Base):
    """
    Column -> Column : column-level lineage. This is what powers
    "which downstream columns are affected if this column breaks" —
    the core of impact analysis.
    """
    __tablename__ = "edge_derives_from"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    source_column_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset_column.id"), nullable=False)
    target_column_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset_column.id"), nullable=False)

    # e.g. "direct copy", "SUM(amount)", "CASE WHEN ... " — the transform
    # expression, extracted from the parsed SQL. Useful context for the
    # root-cause agent and for semantic-duplicate detection.
    transform_expression = SAColumn(Text, nullable=True)
    produced_by_job_id = SAColumn(UUID(as_uuid=True), ForeignKey("job.id"), nullable=True)

    __table_args__ = (
        UniqueConstraint("source_column_id", "target_column_id", "produced_by_job_id"),
    )


class FeedsEdge(Base):
    """Asset -> Dashboard : this asset is used by this dashboard."""
    __tablename__ = "edge_feeds"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    asset_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset.id"), nullable=False)
    dashboard_id = SAColumn(UUID(as_uuid=True), ForeignKey("dashboard.id"), nullable=False)

    __table_args__ = (UniqueConstraint("asset_id", "dashboard_id"),)


class OwnsEdge(Base):
    """Owner -> Asset (or Job) : ownership, for alert routing."""
    __tablename__ = "edge_owns"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    owner_id = SAColumn(UUID(as_uuid=True), ForeignKey("owner.id"), nullable=False)
    asset_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset.id"), nullable=True)
    job_id = SAColumn(UUID(as_uuid=True), ForeignKey("job.id"), nullable=True)


# ---------------------------------------------------------------------------
# Findings — where detection/reasoning layers write their output.
# Everything in Phases 1-3 (quality issues, root causes, recommendations)
# lands here, so the UI/alerting/health-score layers all read one table.
# ---------------------------------------------------------------------------

class FindingType(str, enum.Enum):
    QUALITY_ISSUE = "quality_issue"          # null spike, type drift, duplicate, etc.
    SCHEMA_DRIFT = "schema_drift"
    SEMANTIC_DUPLICATE = "semantic_duplicate"
    COST_INEFFICIENCY = "cost_inefficiency"
    ARCHITECTURE_FLAG = "architecture_flag"
    ROOT_CAUSE = "root_cause"
    RECOMMENDED_FIX = "recommended_fix"


class FindingStatus(str, enum.Enum):
    OPEN = "open"
    CONFIRMED = "confirmed"      # human confirmed this is real/correct
    REJECTED = "rejected"        # human said this is wrong
    RESOLVED = "resolved"
    APPLIED = "applied"          # for fixes: the human approved and applied it


class Finding(Base):
    __tablename__ = "finding"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    finding_type = SAColumn(Enum(FindingType), nullable=False)
    status = SAColumn(Enum(FindingStatus), nullable=False, default=FindingStatus.OPEN)

    # What this finding is about — nullable FKs because a finding can be
    # asset-level, column-level, or job-level.
    asset_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset.id"), nullable=True)
    column_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset_column.id"), nullable=True)
    job_id = SAColumn(UUID(as_uuid=True), ForeignKey("job.id"), nullable=True)

    title = SAColumn(String, nullable=False)
    description = SAColumn(Text, nullable=False)

    # Every non-deterministic finding (semantic, root cause, fix) MUST
    # carry a confidence score and the evidence it was based on —
    # this is what feeds the FR-X.1 trust/track-record layer.
    confidence = SAColumn(Numeric, nullable=True)  # 0-1; null for deterministic findings (they're exact)
    evidence = SAColumn(JSONB, nullable=True)       # links/refs to jobs, diffs, log lines, queries used

    # For RECOMMENDED_FIX findings specifically:
    proposed_sql_diff = SAColumn(Text, nullable=True)
    dry_run_validated = SAColumn(Boolean, nullable=True)

    estimated_cost_savings = SAColumn(Numeric, nullable=True)

    detected_at = SAColumn(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    resolved_at = SAColumn(DateTime(timezone=True), nullable=True)
    alerted_at = SAColumn(DateTime(timezone=True), nullable=True)
    reviewed_by_owner_id = SAColumn(UUID(as_uuid=True), ForeignKey("owner.id"), nullable=True)

    __table_args__ = (
        Index("ix_finding_asset_id", "asset_id"),
        Index("ix_finding_type_status", "finding_type", "status"),
    )


# ---------------------------------------------------------------------------
# Health Score
# ---------------------------------------------------------------------------

class HealthScoreScope(str, enum.Enum):
    ASSET = "asset"
    SCHEMA = "schema"
    GLOBAL = "global"


class HealthScoreSnapshot(Base):
    __tablename__ = "health_score_snapshot"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    asset_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset.id"), nullable=True)
    scope = SAColumn(Enum(HealthScoreScope), nullable=False)
    score = SAColumn(Numeric, nullable=False)
    min_child_score = SAColumn(Numeric, nullable=True)
    snapshot_time = SAColumn(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_health_score_scope_time", "scope", "snapshot_time"),
    )