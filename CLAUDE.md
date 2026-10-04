# AI Data Engineer — project guide for Claude

**Current stage:** Stage 1.2 — planning (messy test lab + benchmark). Stage 1.1 ✅ done 2026-10-04: 50/50 tests pass (0 skipped), `alembic upgrade head` creates all 13 tables locally. Design: [`docs/design/metadata_store.md`](docs/design/metadata_store.md).

**Environment:** Python 3.12 (conda env `py312`). Docker Desktop installed (2026-10-04). Git remote to be connected later.
**Collaboration:** Claude proposes plans and writes code only after approval; the user runs installs/tests and Claude gives exact commands.

Vision & full design: [`docs/product/vision_and_roadmap.md`](docs/product/vision_and_roadmap.md). Historical source: [`docs/PROJECT_BRIEF_AI_Data_Engineer.md`](docs/PROJECT_BRIEF_AI_Data_Engineer.md). It assumed "Snowflake + dbt only". **That is superseded by this file.** Its design ideas (versioning, findings, health score, human approval) still apply.

---

## What we're building
A **database-agnostic, AI-assisted anomaly detection engine** for any data layer: transactional/OLTP databases (Postgres, MySQL, SQL Server, Oracle) and warehouses (Snowflake, BigQuery, …). Point it at a database and it:
1. **Discovers** structure and relationships, even when they're undocumented and messy: undeclared FKs, inconsistent names, legacy schemas, daily ETL loads.
2. **Learns normal** from the data's own history.
3. **Finds the widest possible range of anomalies** (taxonomy below), each with evidence.
4. **Explains** impact (what's connected) and, later, root cause; proposes fixes for **human approval**.

Pitch: point it at any database, however messy, and it tells you what's wrong, what it breaks, and why, with evidence, and gets smarter as you confirm or reject findings.

## Anomaly taxonomy (what "maximum kinds" means)
| # | Category | Examples | Method |
|---|---|---|---|
| 1 | Structural | missing PKs, unindexed FK columns, same concept with different types, schema drift | metadata rules |
| 2 | Relational / integrity | undeclared and misnamed FKs, orphans, cardinality changes, duplicate entities | relationship discovery → checks |
| 3 | Column values | nulls, bad formats, out-of-range, impossible values, inconsistent categories | profiling + learned baselines + pattern inference |
| 4 | Business rules | `ship_date < order_date`, totals ≠ sum of lines, invalid status transitions | AI proposes → human approves → deterministic SQL |
| 5 | Time series | volume drops, staleness, failed loads, distribution shift (seasonality-aware) | statistics on measurement history |
| 6 | Row-level outliers | a transaction 100× normal, unusual activity | ML on features computed in-DB |
| 7 | Cross-system | source DB vs warehouse copy disagree after ETL | reconciliation across adapters |
| 8 | Semantic | PII in unexpected places, same meaning under different names | heuristics + AI with confidence |
| 9 | DB health *(add-on, later)* | slow queries, bloat, locks, unused indexes | system catalogs/stats views |

## Who defines the rules
- **We (code):** the *kinds* of checks. **Config:** default sensitivity, overridable per tenant/source/table/column. No hardcoded thresholds about anyone's data.
- **The data:** what "normal" is, through learned baselines per column/table.
- **AI:** *proposes* relationships and business rules from schema + profiles (+ samples only if opted in).
- **Humans:** approve/reject proposals and findings. Approved rules run as plain deterministic SQL. Feedback tunes sensitivity.

**AI discovers, humans approve, the engine enforces.**

## Hard rules
- **Read-only against customer databases.** Read-only transactions, `statement_timeout`, sampling, off-peak scheduling, read replicas preferred. Never write to customer systems.
- **Compute inside the customer DB**; pull out metadata, aggregates, and IDs of offending rows. Raw values leave only if the customer opts in (per source/table), e.g. for AI sampling. Never log raw values.
- **AI never runs at check time.** Executing checks, statistics, and lineage traversal stay deterministic. AI is used only to propose rules/relationships, interpret semantics, and write explanations — always with confidence + evidence, and always reviewed by a human.
- **No hardcoded secrets.** pydantic-settings (env / `.env`); `.env.example` documents every var. Never commit `.env` or key files.
- **No real external calls in tests**: mock LLMs, Slack, and customer DBs. Use testcontainers for real Postgres.
- Thresholds and weights are named, configurable constants with documented defaults.
- Findings quote actual numbers; every finding traces to evidence.
- Small, testable units. Avoid over-engineering. Markdown docs.

## Architecture principles
- **Adapter per database** behind one interface: introspect metadata, run profiling SQL, list relationships/constraints, read query logs. Use SQLAlchemy inspector + sqlglot to stay dialect-neutral where possible.
- **Core engine is deployment-agnostic.** v1 = **self-hosted** (Docker image inside the customer network; best fit for OLTP). Later = hosted SaaS control plane + agent, Snowflake Native App, etc.
- **Metadata store = our own Postgres** (SQLAlchemy 2.x + Alembic). Recursive CTEs for graph traversal; no graph DB.
- **Schema principles:** stable identity rows + versioned rows (`valid_from`/`valid_to`) + profile time series; edges and findings reference stable keys; `tenant_id` on every table (RLS later); timezone-aware datetimes only (`datetime.now(timezone.utc)`).
- One shared `Finding` table for all outputs; deterministic findings have `confidence = NULL`; AI-derived findings require confidence + evidence.

## Tech stack
Python 3.12 · PostgreSQL (metadata store + first target) · SQLAlchemy 2.x · Alembic · pydantic-settings · pytest + testcontainers · ruff · mypy (strict) · pre-commit · pip + `pyproject.toml` · Makefile · GitHub Actions · sqlglot · (later) scikit-learn for outliers, an LLM API under the `reasoning` extra, FastAPI.

Layout: src-layout, package `ai_data_engineer`, one subpackage per layer; `tests/unit` (no Docker) and `tests/integration` (testcontainers).

## Working process
- One step at a time. Every step has a **"done when"** and ends with passing tests before the next starts.
- **Before each step:** short plan + open questions → wait for approval. **After:** user runs tests → update **Current stage** + **Backlog**.
- **Real data early:** validate each layer on the messy test lab (and later real DBs) before building on it. Each validation round has a written answer key in `validation/answer_keys/`.

## Roadmap
**Stage 0 — Setup.** ✅ Repo, tooling, CI config, local Postgres compose. Remaining: install Docker; push to GitHub so CI runs.

**Stage 1 — Core engine on PostgreSQL (CLI only).**
- 1.1 ✅ Metadata store schema v2 + Alembic: data sources, asset/column identity + versions, profile time series, relationships (declared/inferred, with confidence), rules, ingestion runs, findings. Done when: migrations up/down clean, constraint + graph-traversal tests pass.
- 1.2 **Messy test lab + benchmark:** a seeded generator for a realistic OLTP database (e-commerce/billing) with undeclared/misnamed FKs, legacy naming, composite keys, a nightly "ETL" simulator, an anomaly injector with a written answer key, and a **scorer** (caught / missed / false alarms per category). Done when: one command builds it reproducibly in Docker and the scorer runs.
- 1.3 Postgres adapter: introspection + safe in-DB profiling (read-only txn, timeouts, sampling, `pg_stats` where cheap). Done when: metadata store matches the lab DB by manual comparison.
- 1.4 Relationship discovery: declared FKs, name similarity, value inclusion, query-log joins (`pg_stat_statements`), then orphan + cardinality checks + **auto-documentation** (data dictionary + relationship map, Markdown/Mermaid). Done when: discovers the lab's hidden FKs with ≥ the answer key's expected recall; false positives explainable.
- 1.5 Detection: structural, column-value, time-series (volume, freshness, null rate, distinct/duplicates), cold-start safe. Done when: all injected anomalies caught; false positives explainable.
- 1.6 Findings lifecycle (dedup, status), health score, alerts + quiet-baseline mode. Done when: one command profiles, detects, scores, and alerts sensibly.

**Stage 2 — AI-assisted discovery.** LLM proposes business rules + relationships from schema/profiles → approve/reject → compiled to SQL checks; finding explanations; row-level outliers (ML). Done when: on the lab DB, it proposes most hidden business rules with acceptable precision, and approved rules catch the injected violations.

**Stage 3 — More adapters + cross-system.** MySQL, SQL Server, Snowflake (trial + jaffle_shop already loaded), Oracle; source-vs-warehouse reconciliation; DB-health add-on (Postgres first).

**Stage 4 — Productize.** Rules-as-code export (YAML for customer CI), FastAPI, auth, tenant isolation, web UI (findings approve/reject, rule review, relationship map, health dashboard), onboarding (read-only user setup scripts per DB), scheduler/workers, self-monitoring, audit log, **self-hosted Docker packaging**; later a hosted SaaS control plane.

**Stage 5 — Private beta.** 1–3 design partners on real messy DBs; security doc; track confirm/reject rate of every finding and rule (becomes the visible track record).

**Stage 6 — Root cause + fixes.** Change-aware detection (anomalies vs schema changes/deploys/ETL runs); correlate findings + relationships + schema history + load runs into causal explanations; human-approved fix SQL. Go/no-go: correctly explains 3 real past incidents.

**Stage 7 — Launch.** Website, docs, pricing, billing, SOC 2 prep.

**Later:** link tables to application code; natural-language questions; trust badge; BigQuery/Databricks/MongoDB adapters; warehouse cost diagnosis; privacy-first PII masking lands in Stage 2.

## Parallel non-code reminders
- [ ] Find design partners **during Stage 1** — ideally teams with a messy Postgres/MySQL/SQL Server behind an app.
- [ ] Snowflake trial (account `AKZSZEW-PD17196`, jaffle_shop loaded) expires ~30 days after 2026-10-04; a new trial is fine. Used again in Stage 3.

## Backlog / known issues
- Per-source connection config (how `data_source.connection_ref` resolves to credentials) is designed in Stage 1.3.
- Snowflake read-only user (AIDE_SVC) + smoke test deferred to Stage 3 (scripts in `scripts/snowflake/`, guide in `docs/setup/snowflake_setup.md`).
- pre-commit hook revisions pinned to older versions; run `pre-commit autoupdate` once.
- `validation/jaffle_shop/` is a git-ignored nested clone pinned to branch `aide-dbt1` (commit `7d0d8de`, dbt 1.x).
- Rename policy for stable keys (ADR 0001) to be decided in Stage 1.3.
- Job/ETL-run tracking (ADR 0002, superseded) returns with query-log reading in Stage 1.4+.
- Migrations live in `src/ai_data_engineer/migrations/` without `__init__.py`; make sure they ship in the Docker image when packaging (Stage 4).
