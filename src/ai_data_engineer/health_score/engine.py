from datetime import datetime
from typing import Dict, List, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func

from ai_data_engineer.graph.models import (
    Asset, Finding, FindingStatus, HealthScoreSnapshot, HealthScoreScope
)
from ai_data_engineer.health_score.calculator import calculate_score

class HealthScoreEngine:
    def __init__(self, session: Session):
        self.session = session

    def compute_health_scores(self):
        """
        Computes ASSET, SCHEMA, and GLOBAL health scores, 
        saving the snapshots to the database.
        """
        now = datetime.utcnow()
        
        # 1. Gather all active assets
        active_assets = self.session.query(Asset).filter(Asset.valid_to == None, Asset.is_deleted == False).all()
        if not active_assets:
            return

        # 2. Get all OPEN findings for these assets
        asset_ids = [a.id for a in active_assets]
        open_findings = (
            self.session.query(Finding)
            .filter(Finding.status == FindingStatus.OPEN)
            .filter(Finding.asset_id.in_(asset_ids))
            .all()
        )
        
        findings_by_asset = {aid: [] for aid in asset_ids}
        for f in open_findings:
            if f.asset_id:
                findings_by_asset[f.asset_id].append(f.finding_type)

        # 3. Compute ASSET level scores
        asset_scores: Dict[str, float] = {}
        for asset in active_assets:
            score = calculate_score(findings_by_asset[asset.id])
            asset_scores[asset.id] = score
            
            snapshot = HealthScoreSnapshot(
                asset_id=asset.id,
                scope=HealthScoreScope.ASSET,
                score=score,
                snapshot_time=now
            )
            self.session.add(snapshot)

        # 4. Compute SCHEMA level scores
        schema_groups: Dict[Tuple[str, str], List[float]] = {}
        for asset in active_assets:
            key = (asset.database_name, asset.schema_name)
            if key not in schema_groups:
                schema_groups[key] = []
            schema_groups[key].append(asset_scores[asset.id])

        for (db, schema), scores in schema_groups.items():
            if not scores:
                continue
            avg_score = sum(scores) / len(scores)
            min_score = min(scores)
            
            snapshot = HealthScoreSnapshot(
                scope=HealthScoreScope.SCHEMA,
                score=avg_score,
                min_child_score=min_score,
                snapshot_time=now
            )
            self.session.add(snapshot)

        # 5. Compute GLOBAL score
        all_scores = list(asset_scores.values())
        if all_scores:
            avg_global = sum(all_scores) / len(all_scores)
            min_global = min(all_scores)
            
            snapshot = HealthScoreSnapshot(
                scope=HealthScoreScope.GLOBAL,
                score=avg_global,
                min_child_score=min_global,
                snapshot_time=now
            )
            self.session.add(snapshot)

        self.session.commit()
