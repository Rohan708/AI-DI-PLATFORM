# 0003 — `tenant_id` on every table from day one

**Status:** Accepted (implemented in Stage 1.1; RLS in Stage 3)

## Context
This is a multi-tenant SaaS. Retrofitting tenancy after data exists is costly and error-prone.

## Decision
Every table has a non-null `tenant_id`, and every stable key and unique constraint is scoped by tenant. Postgres row-level security will be enabled in Stage 3, when the API exists. Until then, the application filters by tenant explicitly.

## Consequences
- Indexes lead with `tenant_id`.
- Nothing is shared across tenants: no shared embeddings, caches, or indexes.
