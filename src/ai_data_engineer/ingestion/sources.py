"""Registering the databases we watch. A self-hosted install uses one default tenant."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.graph.models import DataSource, SourceKind, Tenant
from ai_data_engineer.ingestion.settings import ScanSettings

DEFAULT_TENANT_NAME = "default"


class SourceNotFoundError(LookupError):
    pass


def default_tenant(session: Session) -> Tenant:
    tenant = session.scalar(select(Tenant).where(Tenant.name == DEFAULT_TENANT_NAME))
    if tenant is None:
        tenant = Tenant(name=DEFAULT_TENANT_NAME)
        session.add(tenant)
        session.flush()
    return tenant


def add_source(
    session: Session,
    *,
    name: str,
    kind: SourceKind,
    connection_ref: str,
    settings: dict[str, Any] | None = None,
) -> DataSource:
    """Register a database. ``settings`` are validated now, so mistakes fail early."""
    validated = ScanSettings.model_validate(settings or {})
    source = DataSource(
        tenant_id=default_tenant(session).id,
        name=name,
        kind=kind,
        connection_ref=connection_ref,
        settings=validated.model_dump(mode="json", exclude_defaults=True),
    )
    session.add(source)
    session.flush()
    return source


def get_source(session: Session, name: str) -> DataSource:
    source = session.scalar(
        select(DataSource)
        .join(Tenant, Tenant.id == DataSource.tenant_id)
        .where(Tenant.name == DEFAULT_TENANT_NAME, DataSource.name == name)
    )
    if source is None:
        raise SourceNotFoundError(f"no data source named {name!r}; add it with `aide source add`")
    return source


def list_sources(session: Session) -> list[DataSource]:
    return list(session.scalars(select(DataSource).order_by(DataSource.name)))


def scan_settings(source: DataSource) -> ScanSettings:
    return ScanSettings.model_validate(source.settings)
