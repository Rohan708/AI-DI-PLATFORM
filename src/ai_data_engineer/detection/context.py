"""Everything checks need, loaded once per detection run: the source's current
structure (catalog), the latest scan and the scans before it, measurement series per
table and column, and current relationships."""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.detection.settings import DetectionSettings
from ai_data_engineer.discovery.catalog import Catalog, load_catalog
from ai_data_engineer.discovery.store import current_relationships
from ai_data_engineer.graph.models import (
    AssetProfile,
    ColumnProfile,
    DataSource,
    IngestionRun,
    Relationship,
    RunStatus,
    finding_fingerprint,
    utcnow,
)

USABLE_RUNS = (RunStatus.SUCCEEDED, RunStatus.PARTIAL)


@dataclass
class DetectionContext:
    session: Session
    source: DataSource
    settings: DetectionSettings
    catalog: Catalog
    now: datetime
    current_run_id: uuid.UUID | None
    asset_profiles: dict[uuid.UUID, list[AssetProfile]] = field(default_factory=dict)
    column_profiles: dict[uuid.UUID, list[ColumnProfile]] = field(default_factory=dict)
    relationships: list[Relationship] = field(default_factory=list)

    @property
    def event_since(self) -> datetime:
        return self.now - timedelta(days=self.settings.event_lookback_days)

    def asset_series(self, asset_key: uuid.UUID) -> tuple[list[AssetProfile], AssetProfile | None]:
        """(earlier profiles oldest-first, profile from the current scan or None)."""
        return self._split(self.asset_profiles.get(asset_key, []))

    def column_series(
        self, column_key: uuid.UUID
    ) -> tuple[list[ColumnProfile], ColumnProfile | None]:
        return self._split(self.column_profiles.get(column_key, []))

    def enough_history(self, history: Sequence[object]) -> bool:
        return len(history) >= self.settings.min_history_scans

    def _split[P: (AssetProfile, ColumnProfile)](self, series: list[P]) -> tuple[list[P], P | None]:
        if series and series[-1].ingestion_run_id == self.current_run_id:
            return series[:-1][-self.settings.baseline_scans :], series[-1]
        return series[-self.settings.baseline_scans :], None


def fingerprint(check: str, *subject: object) -> str:
    return finding_fingerprint(check, *subject)


def build_context(
    session: Session, source: DataSource, settings: DetectionSettings, now: datetime | None
) -> DetectionContext:
    runs = list(
        session.scalars(
            select(IngestionRun)
            .where(IngestionRun.data_source_id == source.id, IngestionRun.status.in_(USABLE_RUNS))
            .order_by(IngestionRun.started_at.desc())
            .limit(settings.baseline_scans + 1)
        )
    )
    current = runs[0] if runs else None
    ctx = DetectionContext(
        session=session,
        source=source,
        settings=settings,
        catalog=load_catalog(session, source),
        now=now or (current.started_at if current else utcnow()),
        current_run_id=current.id if current else None,
    )
    run_ids = [r.id for r in runs]
    if not run_ids:
        return ctx

    assets: dict[uuid.UUID, list[AssetProfile]] = defaultdict(list)
    for ap in session.scalars(
        select(AssetProfile)
        .where(AssetProfile.ingestion_run_id.in_(run_ids))
        .order_by(AssetProfile.measured_at)
    ):
        assets[ap.asset_key].append(ap)
    columns: dict[uuid.UUID, list[ColumnProfile]] = defaultdict(list)
    for cp in session.scalars(
        select(ColumnProfile)
        .where(ColumnProfile.ingestion_run_id.in_(run_ids))
        .order_by(ColumnProfile.measured_at)
    ):
        columns[cp.column_key].append(cp)
    ctx.asset_profiles = dict(assets)
    ctx.column_profiles = dict(columns)
    ctx.relationships = current_relationships(session, ctx.catalog)
    return ctx
