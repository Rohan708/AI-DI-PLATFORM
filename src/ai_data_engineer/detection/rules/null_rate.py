import statistics
from sqlalchemy.orm import Session
from sqlalchemy import select, and_, desc
from uuid import UUID

from ai_data_engineer.graph.models import Asset, AssetColumn, FindingType
from ai_data_engineer.detection.rules.base import BaseCheck, FindingResult, register_check
from ai_data_engineer.config import settings


class NullRateSpikeCheck(BaseCheck):
    """
    Detects if the null rate of a column jumps significantly compared to its trailing baseline.
    """

    def run(self, session: Session, asset_id: UUID | None = None) -> list[FindingResult]:
        findings = []
        trailing_n = settings.detection_trailing_versions
        abs_threshold = settings.detection_null_rate_abs_threshold
        z_threshold = settings.detection_null_rate_zscore_threshold

        asset_query = select(Asset).where(Asset.valid_to.is_(None))
        if asset_id:
            asset_query = asset_query.where(Asset.id == asset_id)
        
        active_assets = session.scalars(asset_query).all()

        for asset in active_assets:
            for active_col in asset.columns:
                if active_col.null_rate is None:
                    continue
                
                # Fetch history for this logical column
                history_query = (
                    select(AssetColumn.null_rate)
                    .join(Asset, Asset.id == AssetColumn.asset_id)
                    .where(
                        and_(
                            Asset.source_system == asset.source_system,
                            Asset.database_name == asset.database_name,
                            Asset.schema_name == asset.schema_name,
                            Asset.table_name == asset.table_name,
                            AssetColumn.column_name == active_col.column_name,
                            AssetColumn.id != active_col.id,
                            AssetColumn.null_rate.is_not(None)
                        )
                    )
                    .order_by(desc(AssetColumn.valid_from))
                    .limit(trailing_n)
                )
                
                historical_rates = session.scalars(history_query).all()
                if not historical_rates:
                    continue
                
                # Convert to float for math
                historical_rates = [float(r) for r in historical_rates]
                current_rate = float(active_col.null_rate)
                
                avg_rate = statistics.mean(historical_rates)
                
                is_anomaly = False
                trigger_reason = ""
                
                # Check absolute jump
                if (current_rate - avg_rate) > abs_threshold:
                    is_anomaly = True
                    trigger_reason = f"Absolute jump of {(current_rate - avg_rate)*100:.1f}% exceeds threshold of {abs_threshold*100:.1f}%."
                
                # Check z-score if we have enough variance
                elif len(historical_rates) > 1:
                    stdev = statistics.stdev(historical_rates)
                    if stdev > 0:
                        z_score = (current_rate - avg_rate) / stdev
                        if z_score > z_threshold:
                            is_anomaly = True
                            trigger_reason = f"Z-score of {z_score:.2f} exceeds threshold of {z_threshold}."

                if is_anomaly:
                    findings.append(
                        FindingResult(
                            title=f"Null rate spike detected for column '{active_col.column_name}'",
                            description=(
                                f"Null rate for {active_col.column_name} jumped from "
                                f"a baseline of {avg_rate*100:.1f}% to {current_rate*100:.1f}%. {trigger_reason}"
                            ),
                            finding_type=FindingType.QUALITY_ISSUE,
                            asset_id=asset.id,
                            column_id=active_col.id,
                            evidence={
                                "current_null_rate": current_rate,
                                "baseline_avg": avg_rate,
                                "historical_values": historical_rates,
                                "trigger_reason": trigger_reason,
                            }
                        )
                    )

        return findings

register_check(NullRateSpikeCheck())
