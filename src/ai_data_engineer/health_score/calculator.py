from typing import List
from ai_data_engineer.graph.models import FindingType
from ai_data_engineer.config import settings

def _get_weight(finding_type: FindingType) -> int:
    weights = {
        FindingType.QUALITY_ISSUE: settings.health_score_weight_quality,
        FindingType.SCHEMA_DRIFT: settings.health_score_weight_schema,
        FindingType.SEMANTIC_DUPLICATE: settings.health_score_weight_semantic,
        FindingType.ARCHITECTURE_FLAG: settings.health_score_weight_architecture,
        FindingType.COST_INEFFICIENCY: settings.health_score_weight_cost,
        FindingType.ROOT_CAUSE: 0,
        FindingType.RECOMMENDED_FIX: 0,
    }
    return weights.get(finding_type, 0)

def calculate_score(finding_types: List[FindingType]) -> float:
    """
    Computes a health score based purely on a list of finding types.
    Subtracts weights from 100, bounded at 0.
    """
    penalty = sum(_get_weight(ft) for ft in finding_types)
    return float(max(0, 100 - penalty))
