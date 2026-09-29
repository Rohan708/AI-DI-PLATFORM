# Metadata Graph — Design Notes

## Why this schema, these choices

**Postgres, not a graph DB.** At your scale (one customer's warehouse — thousands to low millions of columns/edges, not billions), a recursive CTE over indexed foreign keys performs fine and you get transactions, tooling, and hiring pool for free. Revisit only if a specific traversal proves too slow in practice — don't pre-optimize for a scale problem you don't have yet.

**Every node is time-versioned (`valid_from`/`valid_to`).** Instead of UPDATE-in-place, each change inserts a new row and closes the old one. This means schema-drift detection is just "diff the current row against the last one for this natural key" — no separate audit/history table needed, and it directly feeds root-cause investigation later ("what changed right before this broke?").

**`Job` is not versioned like other nodes — every execution is its own row.** This table *is* your run-history log. Root-cause investigation queries this directly: "did a job touching this asset fail or behave differently around the time the issue appeared?"

**Edges are separate tables, not foreign keys on nodes.** `DERIVES_FROM` (column-to-column lineage) needs its own attributes (the transform expression, which job produced it) — this is what lets the root-cause agent explain *how* a downstream column is broken, not just *that* it's connected.

**`Finding` is the single output table for every detection/reasoning layer.** Quality issues, schema drift, semantic duplicates, cost flags, root causes, and recommended fixes all land here, with the same shape: confidence score + evidence (JSONB) + status. This is deliberate — it means your UI, alerting, and the trust/track-record layer (FR-X.1 from the SRS) all read from one place, regardless of which detection layer produced the finding. Deterministic findings (null spike, type drift) simply have `confidence = NULL` since they're exact; anything LLM-derived (semantic match, root cause, fix) must always carry a confidence score and evidence — no exceptions, since that's what the trust layer depends on.

## What's NOT in this schema yet (by design)

- **Raw data / row-level values** — deliberately absent. `min_value_repr`/`max_value_repr` on `AssetColumn` are stored as text representations for profiling, not actual sensitive values. This matches the "push compute into the warehouse, pull out only metadata" security posture.
- **Data contracts (FR-3.3)** — comes once you're building the CI-check feature; don't design it now.
- **Multi-tenant isolation** — this schema is single-tenant as drafted. Before any real customer, every table needs a `tenant_id` column and row-level security (Postgres RLS) or fully separate schemas per tenant. Flagging this now so it's not forgotten, but it's a Step-1.5 concern, not needed for the spike.

## Suggested build order against this schema

1. **Ingestion adapter (Snowflake + dbt)** — writes `Asset`, `AssetColumn`, `Job`, and the `PRODUCES`/`READS_FROM`/`DERIVES_FROM` edges. This is Step 0/1 from the roadmap — point it at your design partner's real warehouse and see what breaks in the model.
2. **Deterministic check engine** — reads `AssetColumn` history (via `valid_from`/`valid_to`), writes `Finding` rows of type `QUALITY_ISSUE` / `SCHEMA_DRIFT`.
3. **Lineage queries** (see `lineage_queries.py`) — power impact analysis (`FeedsEdge` → `Dashboard`) and later, root-cause traversal.
4. Everything after that (health score, alerting, semantic matching, root cause, fix-gen) reads/writes this same graph — no schema changes should be needed for Phase 1–2 of the roadmap.

## Files here

- `models.py` — SQLAlchemy models (the schema itself)
- `lineage_queries.py` — upstream/downstream recursive CTE traversal, the core query pattern for impact analysis and root-cause