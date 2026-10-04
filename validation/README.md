# Validation against real data

This folder holds the material we use to check each layer against real or realistic data before building on top of it. The lab itself lives in the package (`ai_data_engineer.lab`, see `docs/design/test_lab.md`); its outputs land here.

- `jaffle_shop/`: the Snowflake/dbt sample project (Stage 3), cloned here (see `docs/setup/snowflake_setup.md`). The whole folder is git-ignored because it is its own git repo.
- `reports/`: benchmark reports from `aide lab score` (caught / missed / false alarms).
- `answer_keys/`: for each validation round, a written list of the problems we injected and the findings we expect. Write it **before** running detection.

Each answer key is one Markdown file (`YYYY-MM-DD-<round>.md`) containing:
1. Baseline ingest timestamp
2. The injected problems: the exact SQL and the affected table/column
3. The expected finding type for each problem
4. After the run: actual findings, misses, false positives with an explanation, and the thresholds we changed
