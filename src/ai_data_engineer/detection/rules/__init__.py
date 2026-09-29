# Import all check modules to trigger their register_check() calls
from .null_rate import NullRateSpikeCheck
from .type_drift import TypeDriftCheck
from .schema_drift import SchemaDriftCheck
from .distinct_count import DistinctCountAnomalyCheck
from .base import get_all_checks, BaseCheck, FindingResult

__all__ = [
    "NullRateSpikeCheck",
    "TypeDriftCheck",
    "SchemaDriftCheck",
    "DistinctCountAnomalyCheck",
    "get_all_checks",
    "BaseCheck",
    "FindingResult",
]
