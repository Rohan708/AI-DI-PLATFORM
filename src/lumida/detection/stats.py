"""Robust statistics for "is this value unusual compared with its own history?".

Median and MAD (median absolute deviation) instead of mean and standard deviation: one odd
day in the history doesn't distort what "normal" means.
"""

from collections.abc import Sequence
from statistics import median

# Scales MAD to be comparable with a standard deviation for normally distributed data.
MAD_TO_SIGMA = 1.4826


def mad(values: Sequence[float]) -> float:
    centre = median(values)
    return median(abs(v - centre) for v in values)


def robust_z(value: float, history: Sequence[float], min_spread: float) -> float:
    """How many "typical spreads" ``value`` sits above the history's median."""
    spread = max(MAD_TO_SIGMA * mad(history), min_spread)
    return (value - median(history)) / spread
