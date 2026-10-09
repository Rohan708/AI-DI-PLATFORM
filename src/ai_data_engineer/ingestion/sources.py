"""Registering the databases we watch. A self-hosted install uses one default tenant."""

from typing import Any

from sqlalchemy import CursorResult, delete, or_, select
from sqlalchemy.orm import Session

from ai_data_engineer.graph.models import (
    Asset,
    AssetColumn,
    AssetColumnVersion,
    AssetProfile,
    AssetVersion,
    ColumnProfile,
    DataSource,
    Finding,
    HealthSnapshot,
    IngestionRun,
    QueryJoin,
    ReconciliationPair,
    Relationship,
    Rule,
    SourceKind,
    Tenant,
)
from ai_data_engineer.ingestion.settings import ScanSettings

DEFAULT_TENANT_NAME = "default"


class SourceNotFoundError(LookupError):
    pass


class SourceExistsError(ValueError):
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
    tenant = default_tenant(session)
    if session.scalar(
        select(DataSource.id).where(DataSource.tenant_id == tenant.id, DataSource.name == name)
    ):
        raise SourceExistsError(f"a data source named {name!r} already exists")
    source = DataSource(
        tenant_id=tenant.id,
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


def remove_source(session: Session, source: DataSource) -> dict[str, int]:
    """Delete everything the metadata store knows about a source (its scans, structure,
    measurements, relationships, findings, scores), then the source itself. Returns the
    number of rows deleted per table. The customer's database is never touched."""
    assets = select(Asset.asset_key).where(Asset.data_source_id == source.id)
    columns = select(AssetColumn.column_key).where(AssetColumn.asset_key.in_(assets))
    runs = select(IngestionRun.id).where(IngestionRun.data_source_id == source.id)
    # Order matters: children before the rows they reference. Finding events and
    # relationship columns go with their parents (ON DELETE CASCADE).
    steps = [
        (
            "reconciliation_pair",
            delete(ReconciliationPair).where(
                or_(
                    ReconciliationPair.left_source_id == source.id,
                    ReconciliationPair.right_source_id == source.id,
                )
            ),
        ),
        ("finding", delete(Finding).where(Finding.data_source_id == source.id)),
        (
            "health_snapshot",
            delete(HealthSnapshot).where(HealthSnapshot.data_source_id == source.id),
        ),
        ("rule", delete(Rule).where(Rule.data_source_id == source.id)),
        ("query_join", delete(QueryJoin).where(QueryJoin.data_source_id == source.id)),
        (
            "relationship",
            delete(Relationship).where(
                or_(Relationship.from_asset_key.in_(assets), Relationship.to_asset_key.in_(assets))
            ),
        ),
        ("column_profile", delete(ColumnProfile).where(ColumnProfile.column_key.in_(columns))),
        ("asset_profile", delete(AssetProfile).where(AssetProfile.asset_key.in_(assets))),
        (
            "asset_column_version",
            delete(AssetColumnVersion).where(AssetColumnVersion.column_key.in_(columns)),
        ),
        ("asset_version", delete(AssetVersion).where(AssetVersion.asset_key.in_(assets))),
        ("asset_column", delete(AssetColumn).where(AssetColumn.asset_key.in_(assets))),
        ("asset", delete(Asset).where(Asset.data_source_id == source.id)),
        ("ingestion_run", delete(IngestionRun).where(IngestionRun.id.in_(runs))),
    ]
    counts = {}
    for table, statement in steps:
        executed = session.execute(statement, execution_options={"synchronize_session": False})
        # A DELETE always returns a CursorResult; the check narrows the type for mypy.
        counts[table] = executed.rowcount if isinstance(executed, CursorResult) else 0
    session.delete(source)
    session.flush()
    return counts
