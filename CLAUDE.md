# Lumida — project guide for Claude

**Current stage:** Stage 2.6–2.7 ✅ (2026-10-11): personal-data map + PII in unexpected places (`lumida pii`, nightly `semantic` step); **context from people**: `lumida ask` (generated questions → `not_null` / `unique` / `allowed_values` rules, freshness deadlines, closed weekdays, criticality-weighted health), `lumida context …`, dbt `schema.yml` import. Stage 3 ✅ (283+ tests, CI green on GitHub). **Next:** retire the leaked key `aide_rsa_key.p8` + Snowflake smoke run (user's part, steps in chat 2026-10-11; trial expires ~2026-11-03) → **Stage 4 plan** (open question: web UI inside the self-hosted image vs separate app; recommended: inside, for v1). Plain-language overview: [`docs/guide/how_it_works.md`](docs/guide/how_it_works.md). Designs: [`metadata_store`](docs/design/metadata_store.md), [`test_lab`](docs/design/test_lab.md), [`postgres_adapter`](docs/design/postgres_adapter.md), [`relationship_discovery`](docs/design/relationship_discovery.md), [`detection`](docs/design/detection.md), [`lifecycle_health_alerts`](docs/design/lifecycle_health_alerts.md), [`ai_rules`](docs/design/ai_rules.md), [`ai_assist`](docs/design/ai_assist.md), [`row_outliers`](docs/design/row_outliers.md), [`stage3_databases`](docs/design/stage3_databases.md), [`semantic_pii`](docs/design/semantic_pii.md), [`context`](docs/design/context.md); DB setup: [`databases`](docs/setup/databases.md).

**Name:** **Lumida** ("light on your data"; chosen 2026-10-14, may still change; the web site will explain the meaning). Command `lumida`, package `lumida`, settings prefix `LUMIDA_` (pre-rename `AIDE_*` names still read). Formerly *AI Data Engineer* / `aide`.
**Environment:** Python 3.12 (conda env `py312`). Docker Desktop installed (2026-10-04). Git: `origin` = github.com/Rohan708/Lumida, work on `main` (since 2026-10-11; `latest` kept in sync).
**Collaboration:** Claude proposes plans and writes code only after approval; the user runs installs/tests and Claude gives exact commands.

Vision & full design: [`docs/product/vision_and_roadmap.md`](docs/product/vision_and_roadmap.md). Selling + plain-language logic: [`docs/product/sales_and_logic.md`](docs/product/sales_and_logic.md). Historical source: [`docs/PROJECT_BRIEF_AI_Data_Engineer.md`](docs/PROJECT_BRIEF_AI_Data_Engineer.md). It assumed "Snowflake + dbt only". **That is superseded by this file.** Its design ideas (versioning, findings, health score, human approval) still apply.

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

Layout: src-layout, package `lumida`, one subpackage per layer; `tests/unit` (no Docker) and `tests/integration` (testcontainers). **Test file names must be unique across both folders** (mypy treats them as top-level modules): unit files use a `_logic` suffix, e.g. `test_detection_logic.py` vs `integration/test_detection.py`.

## Working process
- One step at a time. Every step has a **"done when"** and ends with passing tests before the next starts.
- **Before each step:** short plan + open questions → wait for approval. **After:** user runs tests → update **Current stage** + **Backlog**.
- **Real data early:** validate each layer on the messy test lab (and later real DBs) before building on it. Each validation round has a written answer key in `validation/answer_keys/`.

## Roadmap
**Stage 0 — Setup.** ✅ Repo, tooling, CI config, local Postgres compose, Docker, pushed to GitHub (2026-10-11).

**Stage 1 — Core engine on PostgreSQL (CLI only).**
- 1.1 ✅ Metadata store schema v2 + Alembic: data sources, asset/column identity + versions, profile time series, relationships (declared/inferred, with confidence), rules, ingestion runs, findings. Done when: migrations up/down clean, constraint + graph-traversal tests pass.
- 1.2 ✅ **Messy test lab + benchmark:** a seeded generator for a realistic OLTP database (e-commerce/billing) with undeclared/misnamed FKs, legacy naming, composite keys, a nightly "ETL" simulator, an anomaly injector with a written answer key, and a **scorer** (caught / missed / false alarms per category). Done when: one command builds it reproducibly in Docker and the scorer runs.
- 1.3 ✅ Postgres adapter: introspection + safe in-DB profiling (read-only txn, timeouts, sampling, `pg_stats` where cheap). Done when: metadata store matches the lab DB by manual comparison.
- 1.4 ✅ Relationship discovery: declared FKs, name similarity, value inclusion, query-log joins (`pg_stat_statements`), then orphan + cardinality checks + **auto-documentation** (data dictionary + relationship map, Markdown/Mermaid). Done when: discovers the lab's hidden FKs with ≥ the answer key's expected recall; false positives explainable.
- 1.5 ✅ Detection: structural, column-value, time-series (volume, freshness, null rate, distinct/duplicates), cold-start safe. Done when: all injected anomalies caught; false positives explainable.
- 1.6 ✅ Findings lifecycle (dedup, status), health score, alerts + quiet-baseline mode. Done when: one command profiles, detects, scores, and alerts sensibly. (Lab 2026-10-11: 0 false alarms, quiet nights silent.)

**Stage 2 — AI-assisted discovery.** ✅ (2026-10-11; all AI features run on Gemini) LLM proposes business rules + relationships from schema/profiles → approve/reject → compiled to SQL checks; finding explanations; row-level outliers (ML). Done when: on the lab DB, it proposes most hidden business rules with acceptable precision, and approved rules catch the injected violations.

**Stage 3 — More adapters + cross-system.** ✅ (2026-10-11; Snowflake manual run pending) MySQL, SQL Server, Snowflake (trial + jaffle_shop already loaded), Oracle; source-vs-warehouse reconciliation; DB-health add-on (Postgres first).

**Stage 4 — Productize.** Rules-as-code export (YAML for customer CI), FastAPI, auth, tenant isolation, web UI (findings approve/reject, rule review, relationship map, health dashboard), onboarding (read-only user setup scripts per DB), scheduler/workers, self-monitoring, audit log, **self-hosted Docker packaging**; later a hosted SaaS control plane.

**Stage 5 — Private beta.** 1–3 design partners on real messy DBs; security doc; track confirm/reject rate of every finding and rule (becomes the visible track record).

**Stage 6 — Root cause + fixes.** Change-aware detection (anomalies vs schema changes/deploys/ETL runs); correlate findings + relationships + schema history + load runs into causal explanations; human-approved fix SQL. Go/no-go: correctly explains 3 real past incidents.

**Stage 7 — Launch.** Website, docs, pricing, billing, SOC 2 prep.

**Later:** link tables to application code; natural-language questions; trust badge; BigQuery/Databricks/MongoDB adapters; warehouse cost diagnosis; privacy-first PII masking lands in Stage 2.

## Parallel non-code reminders
- [ ] Find design partners **during Stage 1** — ideally teams with a messy Postgres/MySQL/SQL Server behind an app.
- [ ] Snowflake trial (account `AKZSZEW-PD17196`, jaffle_shop loaded) expires ~30 days after 2026-10-04; a new trial is fine. Used again in Stage 3.

## Backlog / known issues
- `connection_ref` = name of an env var / `.env` entry (ADR 0005); add a secrets-manager backend before hosted SaaS (Stage 4).
- Snowflake read-only user (AIDE_SVC) + smoke test deferred to Stage 3 (scripts in `scripts/snowflake/`, guide in `docs/setup/snowflake_setup.md`).
- pre-commit hook revisions pinned to older versions; run `pre-commit autoupdate` once.
- `validation/jaffle_shop/` is a git-ignored nested clone pinned to branch `aide-dbt1` (commit `7d0d8de`, dbt 1.x).
- Rename detection (a rename currently looks like drop + add) — decide with real data in 1.5/1.6.
- Job/ETL-run tracking (ADR 0002, superseded) returns with query-log reading in Stage 1.4+.
- Lab sources must be registered with `--exclude-schemas aide_lab` (the lab's bookkeeping schema).
- On Windows with Smart App Control, `lumida.exe` and mypy's DLL can be blocked; use `python -m lumida …` and the pure-Python mypy install (docs/setup/local_dev.md).
- History must be recorded in time order: before re-running a lab plan on the same source, `lumida source remove NAME --yes` (1.6).
- Views are described but not profiled (cost); revisit with per-source opt-in.
- Discovery settings are global defaults (`discovery/settings.py`); add per-source overrides. Composite links only found with identical column names.
- Orphan checks run on proposed relationships only at confidence >= 0.9; without `pg_stat_statements`, integer links rarely reach that (no query-log signal) — document for customers.
- Query-log evidence is now remembered (`query_join`, 1.5); stale orphan findings are marked "not re-checked" (1.5). Generalise the not-re-checked marker to all checks in 1.6.
- Detection limits: severity per check (not impact-scaled), freshness needs a timestamp/date column (pg_stat counters stored, unused), volume detects drops only, category variants are lexical (synonyms → Stage 2).
- Alerts: one channel per source (owner routing needs ownership data, Stage 4); no "all clear" messages; health penalties not impact-weighted.
- AI rules (2.2): no expressions (`sum(qty*price)`), allowed-values or conditional rules yet; per-source opt-in to share sample values with the LLM; prompt capped at 200 tables (no chunking); proposals aren't refreshed automatically on schema change. Only Gemini implemented; add Claude/others behind `reasoning/llm.py`.
- Row outliers (2.5): one column at a time, high side only; multi-column ML needs a per-table opt-in (row features leave the DB). Outlier findings of dropped columns aren't auto-resolved.
- Stage 3: generic adapter samples the *first* N rows above 1M (not random); SQL Server has no session statement timeout; DB health is Postgres-only; reconciliation compares aggregates only (key-level hash comparison planned); Oracle/SQL Server verified only by SQL compilation until a real run.
- Semantic/PII (2.6): lexical patterns only (no Luhn check), no detection of names/addresses/birth dates from values (needs opt-in AI), SQL Server unsupported (no regex), first 10k rows per table.
- Context (2.7): no status-transition rules (need row-change history); terminal questions only (web form in Stage 4); AI doesn't draft descriptions yet; deadlines need a time column that moves.
- Rename (2026-10-14): kept on purpose — local DB user/name `aide` (docker-compose, `.env`), Snowflake objects `AIDE_SVC` / `AIDE_WH` / `AIDE_READONLY`, `scripts/` role names (`aide_readonly`), the lab's bookkeeping schema `aide_lab`, decision records. GitHub repo renamed to `Rohan708/Lumida` (2026-10-14; old URL redirects).
- Website (2026-10-14): `website/` = static landing page (vanilla HTML/CSS/JS, logo SVGs, brand notes in `website/README.md`); sign-up form not connected (`EARLY_ACCESS_ENDPOINT`); domain not chosen yet. Preview: `.claude/launch.json` → `lumida-website` (python http.server on :8765).
- Migrations live in `src/lumida/migrations/` without `__init__.py`; make sure they ship in the Docker image when packaging (Stage 4).
