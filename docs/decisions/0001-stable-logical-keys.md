# 0001 — Edges and findings reference stable logical keys

**Status:** Accepted (implemented in Stage 1.1)

## Context
Nodes are time-versioned: we insert a new row on change and never update in place. If edges point at version row IDs, every re-ingestion orphans lineage, ownership, and findings on old versions.

## Decision
`Asset` and `AssetColumn` each carry a stable key (`asset_key`, `column_key`) that stays the same across versions. Every edge (DerivesFrom, Produces, ReadsFrom, Feeds, Owns) and every `Finding` references stable keys. Version rows keep `valid_from` and `valid_to` (`NULL` means current).

## Consequences
- Lineage CTEs traverse stable keys. They join to the current version with `valid_to IS NULL`, or to a point-in-time version for history.
- Renames need an explicit policy, decided in Stage 1.2.
