# 0004 — Identity, version, and profile tables

**Status:** Accepted (implemented in Stage 1.1). Refines [0001](0001-stable-logical-keys.md).

## Context
For every table and column we track three things that change at very different rates:
1. **What it is.** Rarely changes, and must never break links.
2. **Its structure** (type, nullability, keys, indexes). Changes occasionally, and every change matters (schema drift).
3. **Its measurements** (row count, null rate, distinct count). Change on every scan; this is the time series that statistical detection reads.

Putting all three in one versioned row would create a new "version" on every scan, because measurements always move. That would bury real structural changes in noise. It would also make "one current row" hard to enforce for foreign keys.

## Decision
Use three tables per level (asset and column):

| Kind | Tables | Written | Read by |
|---|---|---|---|
| **Identity** | `asset`, `asset_column` | once; only `last_seen_at` / `deleted_at` updated | everything (relationships, rules, and findings reference these keys) |
| **Version** | `asset_version`, `asset_column_version` | new row on structural change; old row's `valid_to` closed | schema/type drift, structural checks, root cause |
| **Profile** | `asset_profile`, `column_profile` | one row per scan | statistical checks (null rate, volume, freshness, distinct) |

Rules:
- A partial unique index (`WHERE valid_to IS NULL`) guarantees at most one current version per identity.
- A dropped-and-recreated table or column keeps its identity. The gap is visible in the version history.
- All structural writes go through `graph/versioning.py` (`record_asset`, `mark_missing_assets`), which is idempotent.
- Relationships are versioned the same way, so "how were these tables connected before the incident?" is answerable.

## Consequences
- Foreign keys from relationships, rules, and findings to identities are real database constraints.
- Detection reads clean series: profiles for statistics, versions for drift.
- Renames currently look like "drop + add". A rename policy is decided in Stage 1.3 with real data.
- Supersedes the dbt-centric [0002](0002-job-definition-vs-run.md) for now. Job/ETL-run tracking returns when we read query logs (Stage 1.4+).
