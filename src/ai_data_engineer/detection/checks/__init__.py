"""All detection checks, in the order they run. Add a check by appending to a module's
``CHECKS`` list; nothing else needs to change."""

from ai_data_engineer.detection.checks import structural, timeseries, values
from ai_data_engineer.detection.framework import Check

ALL_CHECKS: list[Check] = [*structural.CHECKS, *values.CHECKS, *timeseries.CHECKS]

__all__ = ["ALL_CHECKS"]
