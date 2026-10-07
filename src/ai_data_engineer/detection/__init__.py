"""Deterministic anomaly detection: structural, column-value and time-series checks that
compare every table and column with its own history, plus finding recording (dedup,
refresh, resolve). No LLM calls. See docs/design/detection.md."""
