"""Measurement history: what statistical detection reads."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.graph.models import AssetProfile, ColumnProfile

DEFAULT_HISTORY_LIMIT = 30


def get_column_profile_history(
    session: Session, column_key: uuid.UUID, *, limit: int = DEFAULT_HISTORY_LIMIT
) -> list[ColumnProfile]:
    """The most recent ``limit`` profiles of a column, newest first."""
    return list(
        session.scalars(
            select(ColumnProfile)
            .where(ColumnProfile.column_key == column_key)
            .order_by(ColumnProfile.measured_at.desc())
            .limit(limit)
        )
    )


def get_asset_profile_history(
    session: Session, asset_key: uuid.UUID, *, limit: int = DEFAULT_HISTORY_LIMIT
) -> list[AssetProfile]:
    """The most recent ``limit`` profiles of a table, newest first."""
    return list(
        session.scalars(
            select(AssetProfile)
            .where(AssetProfile.asset_key == asset_key)
            .order_by(AssetProfile.measured_at.desc())
            .limit(limit)
        )
    )
