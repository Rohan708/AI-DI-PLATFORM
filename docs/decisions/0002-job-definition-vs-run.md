# 0002 — Split Job into JobDefinition and JobRun

**Status:** Superseded for now (2026-10-04). It was dbt-centric; after the scope change, job/ETL-run tracking is deferred until we read query logs (Stage 1.4+). See [0004](0004-identity-version-profile.md).

## Context
dbt lineage describes a *model*, which is stable. Query history describes *executions*. A single `Job` table mixed the two, which meant lineage edges were created once per run.

## Decision
- `JobDefinition` is the stable unit: a dbt model or an Airflow task. Lineage edges (Produces, ReadsFrom, DerivesFrom) attach here.
- `JobRun` holds one row per execution: timing, status, rows, bytes, cost, SQL, and errors. Each run references its definition.

## Consequences
- Cost diagnosis (Stage 2) aggregates `JobRun` rows by definition.
- Root cause (Stage 5) diffs consecutive runs of the same definition.
