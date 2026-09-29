from sqlalchemy.orm import Session
from sqlalchemy import select, and_, desc
from uuid import UUID

from ai_data_engineer.graph.models import Asset, FindingType
from ai_data_engineer.detection.rules.base import BaseCheck, FindingResult, register_check


class SchemaDriftCheck(BaseCheck):
    """
    Detects when a column is added or dropped from an asset compared to its prior version.
    """

    def run(self, session: Session, asset_id: UUID | None = None) -> list[FindingResult]:
        findings = []

        asset_query = select(Asset).where(Asset.valid_to.is_(None))
        if asset_id:
            asset_query = asset_query.where(Asset.id == asset_id)

        active_assets = session.scalars(asset_query).all()

        for active_asset in active_assets:
            prior_asset = session.scalars(
                select(Asset)
                .where(
                    and_(
                        Asset.source_system == active_asset.source_system,
                        Asset.database_name == active_asset.database_name,
                        Asset.schema_name == active_asset.schema_name,
                        Asset.table_name == active_asset.table_name,
                        Asset.valid_to == active_asset.valid_from,
                    )
                )
                .order_by(desc(Asset.valid_to))
                .limit(1)
            ).first()

            if not prior_asset:
                continue

            active_col_names = {c.column_name for c in active_asset.columns}
            prior_col_names = {c.column_name for c in prior_asset.columns}

            # Added columns
            for added_col in (active_col_names - prior_col_names):
                col_obj = next(c for c in active_asset.columns if c.column_name == added_col)
                findings.append(
                    FindingResult(
                        title=f"Column added: '{added_col}'",
                        description=f"A new column '{added_col}' was added to {active_asset.table_name}.",
                        finding_type=FindingType.SCHEMA_DRIFT,
                        asset_id=active_asset.id,
                        column_id=col_obj.id,
                        evidence={"change_type": "column_added", "column_name": added_col}
                    )
                )

            # Dropped columns
            for dropped_col in (prior_col_names - active_col_names):
                # The column doesn't exist in the current version, so column_id is None
                findings.append(
                    FindingResult(
                        title=f"Column dropped: '{dropped_col}'",
                        description=f"The column '{dropped_col}' was dropped from {active_asset.table_name}.",
                        finding_type=FindingType.SCHEMA_DRIFT,
                        asset_id=active_asset.id,
                        evidence={"change_type": "column_dropped", "column_name": dropped_col}
                    )
                )

        return findings

# Register the check
register_check(SchemaDriftCheck())
