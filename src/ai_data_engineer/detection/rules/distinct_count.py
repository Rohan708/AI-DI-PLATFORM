import statistics
from sqlalchemy.orm import Session
from sqlalchemy import select, and_, desc
from uuid import UUID

from ai_data_engineer.graph.models import Asset, AssetColumn, FindingType
from ai_data_engineer.detection.rules.base import BaseCheck, FindingResult, register_check
from ai_data_engineer.config import settings


class DistinctCountAnomalyCheck(BaseCheck):
    """
    Detects if the distinct count drops sharply relative to row_count_estimate, indicating possible duplicates.
    """

    def run(self, session: Session, asset_id: UUID | None = None) -> list[FindingResult]:
        findings = []
        trailing_n = settings.detection_trailing_versions
        drop_threshold = settings.detection_distinct_drop_threshold

        asset_query = select(Asset).where(Asset.valid_to.is_(None))
        if asset_id:
            asset_query = asset_query.where(Asset.id == asset_id)
        
        active_assets = session.scalars(asset_query).all()

        for asset in active_assets:
            if asset.row_count_estimate is None or asset.row_count_estimate == 0:
                continue

            current_row_count = float(asset.row_count_estimate)

            for active_col in asset.columns:
                if active_col.distinct_count is None:
                    continue
                
                current_distinct = float(active_col.distinct_count)
                current_ratio = current_distinct / current_row_count

                # Fetch history
                history_query = (
                    select(AssetColumn.distinct_count, Asset.row_count_estimate)
                    .join(Asset, Asset.id == AssetColumn.asset_id)
                    .where(
                        and_(
                            Asset.source_system == asset.source_system,
                            Asset.database_name == asset.database_name,
                            Asset.schema_name == asset.schema_name,
                            Asset.table_name == asset.table_name,
                            AssetColumn.column_name == active_col.column_name,
                            AssetColumn.id != active_col.id,
                            AssetColumn.distinct_count.is_not(None),
                            Asset.row_count_estimate.is_not(None),
                            Asset.row_count_estimate > 0
                        )
                    )
                    .order_by(desc(AssetColumn.valid_from))
                    .limit(trailing_n)
                )
                
                historical_data = session.execute(history_query).all()
                if not historical_data:
                    continue
                
                historical_ratios = [float(dc) / float(rc) for dc, rc in historical_data]
                avg_ratio = statistics.mean(historical_ratios)
                
                # Check for sharp drop in uniqueness (indicating possible duplicates)
                if (avg_ratio - current_ratio) > drop_threshold:
                    findings.append(
                        FindingResult(
                            title=f"Distinct count anomaly (possible duplicates) in '{active_col.column_name}'",
                            description=(
                                f"Uniqueness ratio for {active_col.column_name} dropped from "
                                f"a baseline of {avg_ratio*100:.1f}% to {current_ratio*100:.1f}%. "
                                f"This signals a possible surge in duplicate rows."
                            ),
                            finding_type=FindingType.QUALITY_ISSUE,
                            asset_id=asset.id,
                            column_id=active_col.id,
                            evidence={
                                "current_uniqueness_ratio": current_ratio,
                                "baseline_avg_ratio": avg_ratio,
                                "historical_ratios": historical_ratios,
                                "current_distinct": current_distinct,
                                "current_row_count": current_row_count,
                            }
                        )
                    )

        return findings

register_check(DistinctCountAnomalyCheck())
