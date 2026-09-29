# Detection Engine

The deterministic detection layer analyzes the history of `Asset` and `AssetColumn` nodes in the metadata graph to uncover data quality anomalies and schema drift, without pushing raw queries down to the warehouse. 

It generates exact, explainable `Finding` rows for each anomaly detected, providing enough statistical evidence to avoid needing further re-runs.

## Architecture
The engine consists of individual check classes that inherit from `BaseCheck` (located in `src/ai_data_engineer/detection/rules/base.py`). Each check is an isolated, testable unit that iterates over the metadata graph and yields `FindingResult` objects. The `runner.py` executes these checks and handles deduplicating the resulting findings against the database.

## Available Checks

### 1. Null-Rate Spike Check (`null_rate.py`)
- **What it does**: Compares a column's current null rate against its trailing N-version historical average. Flags when the absolute difference or the standard deviation (Z-score) exceeds a configured threshold.
- **Default Thresholds**: 
  - Absolute Jump: > 10% (0.10)
  - Z-score: > 3.0
  - Trailing Versions: 7
- **Evidence payload**: Contains the `current_null_rate`, `baseline_avg`, array of `historical_values`, and the string `trigger_reason`.

### 2. Type Drift Check (`type_drift.py`)
- **What it does**: Flags when an active column's `data_type` differs from its immediately preceding version.
- **Evidence payload**: Contains `previous_type`, `current_type`, and `changed_at` timestamp.

### 3. Schema Drift Check (`schema_drift.py`)
- **What it does**: Compares the list of columns currently in an asset with the list of columns in its previous version. Emits findings for dropped columns and added columns.
- **Evidence payload**: Contains the `change_type` (`column_added` or `column_dropped`) and the `column_name`.

### 4. Distinct Count Anomaly (`distinct_count.py`)
- **What it does**: Evaluates the ratio of a column's distinct count to the table's total row count (the uniqueness ratio). If this ratio drops sharply relative to its trailing historical average, it flags a potential influx of duplicate rows.
- **Default Thresholds**:
  - Ratio Drop: > 15% (0.15)
- **Evidence payload**: Contains `current_uniqueness_ratio`, `baseline_avg_ratio`, `historical_ratios`, `current_distinct`, and `current_row_count`.
