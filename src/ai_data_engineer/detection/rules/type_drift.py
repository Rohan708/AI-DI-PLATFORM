from sqlalchemy.orm import Session
from sqlalchemy import select, and_, desc
from uuid import UUID

from ai_data_engineer.graph.models import Asset, AssetColumn, FindingType
from ai_data_engineer.detection.rules.base import BaseCheck, FindingResult, register_check


class TypeDriftCheck(BaseCheck):
    """
    Detects when a column's data type changes compared to its immediately prior version.
    """

    def run(self, session: Session, asset_id: UUID | None = None) -> list[FindingResult]:
        findings = []

        # Find the latest active version of assets
        asset_query = select(Asset).where(Asset.valid_to.is_(None))
        if asset_id:
            asset_query = asset_query.where(Asset.id == asset_id)

        active_assets = session.scalars(asset_query).all()

        for active_asset in active_assets:
            # Find the immediately prior version of this asset
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

            # Load columns for both
            active_cols = {c.column_name: c for c in active_asset.columns}
            prior_cols = {c.column_name: c for c in prior_asset.columns}

            for col_name, current_col in active_cols.items():
                if col_name in prior_cols:
                    prior_col = prior_cols[col_name]
                    if prior_col.data_type != current_col.data_type:
                        findings.append(
                            FindingResult(
                                title=f"Data type drift detected for column '{col_name}'",
                                description=(
                                    f"The data type for {col_name} in {active_asset.table_name} changed "
                                    f"from {prior_col.data_type} to {current_col.data_type} "
                                    f"on {current_col.valid_from.date()}."
                                ),
                                finding_type=FindingType.SCHEMA_DRIFT,
                                asset_id=active_asset.id,
                                column_id=current_col.id,
                                evidence={
                                    "previous_type": prior_col.data_type,
                                    "current_type": current_col.data_type,
                                    "changed_at": current_col.valid_from.isoformat(),
                                }
                            )
                        )

        return findings

# Register the check
register_check(TypeDriftCheck())
