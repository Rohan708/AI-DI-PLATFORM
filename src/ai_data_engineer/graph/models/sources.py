"""Tenants, the databases we watch (data sources), and ingestion runs."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ai_data_engineer.graph.models.base import Base, StrEnumType, TenantScoped, utcnow
from ai_data_engineer.graph.models.enums import RunStatus, SourceKind


class Tenant(Base):
    """One customer. A self-hosted install has exactly one."""

    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class DataSource(TenantScoped, Base):
    """A customer database we watch.

    Credentials are never stored here. ``connection_ref`` names where to find them
    (an env var or secrets-manager key).
    """

    __tablename__ = "data_source"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[SourceKind] = mapped_column(StrEnumType(SourceKind))
    connection_ref: Mapped[str | None] = mapped_column(String(500))
    # Per-source behaviour, e.g. sampling limits, whether top values may be stored.
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    disabled_at: Mapped[datetime | None]
    # Name of the env var / .env entry holding the Slack webhook URL (never the URL itself).
    alert_webhook_ref: Mapped[str | None] = mapped_column(String(500))
    # Set by the first alerting run ("quiet baseline"): findings that existed before this
    # moment were recorded but never alerted.
    baseline_completed_at: Mapped[datetime | None]

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_data_source_tenant_name"),)


class IngestionRun(TenantScoped, Base):
    """One scan of a data source. Versions and profiles point at the run that saw them,
    and detection counts runs to decide whether there is enough history (cold start)."""

    __tablename__ = "ingestion_run"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    data_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_source.id"))
    status: Mapped[RunStatus] = mapped_column(StrEnumType(RunStatus), default=RunStatus.RUNNING)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_ingestion_run_source_started", "data_source_id", "started_at"),)
