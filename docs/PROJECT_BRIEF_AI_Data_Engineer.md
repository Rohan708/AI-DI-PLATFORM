# PROJECT BRIEF — AI Data Intelligence & Optimization Platform ("AI Data Engineer")

> Paste this whole document into a new conversation. It is written so someone with zero prior context can understand the project, what exists, what was decided and why, and what to do next.

---

## 0. How to read this brief (important honesty notes)

- **Two sources of code exist.** (a) Code written directly in the original planning conversation — included in full in §5. (b) Code written afterwards by the user's AI coding copilot (a tool that produces an `implementation_plan.md` for review, then writes code into the repo). The copilot-written code lives in the user's repository and was **never shown** in the planning conversation. This brief describes it from the approved plans only. **First action in the new conversation: paste the current repo files so the assistant works from the real code, not from these descriptions.**
- **Nothing has been validated against real data yet.** All work so far is passing (or assumed to pass) unit/integration tests only. A Snowflake free trial was just created for validation.
- Status labels used below: **[WRITTEN HERE]** = code in this brief; **[COPILOT-BUILT, per user]** = user reported it done; **[PLANNED]** = scoped, not built; **[UNVERIFIED]** = believed done but not confirmed.

---

## 1. Project goal

### What
An AI-powered platform that continuously understands a company's entire data infrastructure and acts like an **AI Data Engineer**: it finds problems, explains them, determines root cause and downstream impact, and recommends or generates fixes — **with human approval before anything is executed**.

### One sentence
Build an AI Data Engineer that continuously audits a company's data stack, discovers problems and inefficiencies, understands their root causes and impact, and recommends or executes the best way to fix and optimize them.

### Long-term vision
Monitoring data → understanding data → investigating problems → optimizing and maintaining the data infrastructure (eventually) autonomously.

### Why / market positioning
- Data observability is a crowded, consolidating market. Known players: Monte Carlo, Anomalo, Metaplane (acquired by Datadog), Select Star (acquired by Snowflake), Bigeye, Sifflet, Soda, Elementary, Acceldata, DQLabs, Atlan, Coalesce Quality, OvalEdge, Lightup.
- Most incumbents stop at **detect + alert** ("here's an anomaly, go fix it"). Detection and lineage are table stakes.
- **The gap / our wedge:** root-cause investigation + human-approved fix generation (SQL / dbt PRs / migrations), plus cost & architecture diagnosis, plus a **visible track record of the AI's own accuracy**.
- Refined positioning: *"AI prevents and diagnoses problems, with a visible, growing track record you can trust"* — rather than *"AI finds and fixes everything."*

---

## 2. Core features / requirements

### 2.1 Original 10 capabilities
1. **Detect data problems** — wrong datatypes, nulls, duplicates, inconsistent values, broken relationships, schema drift, missing/invalid data.
2. **Understand data semantics** — columns/tables with the same meaning, inconsistent business definitions, redundant data, relationships.
3. **Analyze ETLs & pipelines** — inefficient transformations, unnecessary full loads, duplicated logic, pipeline failures, optimization opportunities.
4. **Evaluate database architecture** — partitioning, clustering, indexing, normalization/denormalization, storage strategies.
5. **Optimize cost & performance** — expensive queries, unnecessary storage, inefficient processing, estimated savings.
6. **Lineage & impact** — where a problem originated; which tables, pipelines, dashboards, applications are affected.
7. **Continuously monitor & alert** — notify the right data/database owners.
8. **Investigate root causes** — drill down across the stack automatically to explain why.
9. **Recommend and generate fixes** — SQL, ETL/dbt changes, schema migrations, optimization plans; **human approval before execution**.
10. **Data Health Score** — continuously updated view of quality, reliability, efficiency, security, cost.

### 2.2 Changes agreed after review
**De-scoped / reframed:**
- **Architecture recommendations → diagnostic, not prescriptive.** Flag anti-patterns with evidence (e.g. "90% of scans filter on `created_at`; table is not partitioned on it"), let humans decide. Never auto-generate architecture-level fixes without human-defined constraints.
- **Duplicated ETL logic detection** → pushed to a later phase (high cost, nice-to-have).
- **"Human-in-the-loop, always"** is a stated design principle and a selling point, not a hidden implementation detail.

**Added:**
- **Shift-left data contracts** — declared schema/type/null-rate/volume expectations per table, enforced as a CI check (e.g. GitHub Action) on pipeline PRs before merge. "Prevent," not just "detect."
- **PII / sensitive-data detection + access governance** — flag likely-PII columns, report access patterns. Opens a second buyer (security/compliance).
- **Confidence / track-record layer** — every AI-derived finding carries a confidence score + evidence; the UI shows historical human confirm/reject rates per finding type.
- **Business-user trust indicator** — simple "is this dashboard/dataset trustworthy right now" badge for analysts, derived from the health score.

### 2.3 SRS (functional requirements, phased)
**User classes:** Data Engineer, Analytics Engineer, Data Platform Lead/DBA, Data consumer (analyst/BI user), Security/Compliance.

**Phase 1 — Foundation (Observe & Understand).** One warehouse (Snowflake), dbt, read-only.
- FR-1.1 Quality detection: null-rate anomalies vs baseline; datatype mismatch/drift; duplicates; inconsistent categorical values ("US"/"USA"/"United States"); broken referential relationships (orphaned FKs); schema drift (added/removed/renamed/retyped columns).
- FR-1.2 Lineage & impact: column-level lineage from dbt manifest + SQL parsing (sqlglot); downstream tables/dashboards/jobs affected; trace origin upstream.
- FR-1.3 Monitoring & alerting: scheduled + event-triggered checks; route to owners via Slack/email/PagerDuty; suppression/snooze; dedup of repeated alerts.
- FR-1.4 Health score: composite per table/domain; trend over time.
- FR-1.5 Semantic understanding (initial): likely duplicate/overlapping columns/tables via name/type/profile similarity + LLM review; confidence score; human confirmation required.

**Phase 2 — Diagnose (Cost & Root Cause).**
- FR-2.1 Cost/performance: ingest query history + credit usage; top-N expensive queries/jobs; unnecessary full scans/loads; estimated savings per inefficiency.
- FR-2.2 Root cause: correlate lineage + schema change history + pipeline run logs + recent query/code diffs into a candidate causal chain; human-readable narrative with linked evidence; record human feedback (confirmed/rejected/partial).
- FR-2.3 Architecture diagnostics: query-pattern analysis → evidence-based partition/cluster/index flags, no mandatory prescription.
- FR-2.4 PII detection: names/types/(permitted) samples → likely-PII flags; access patterns where logs exist.

**Phase 3 — Act (Recommend & Generate).**
- FR-3.1 Fix generation: SQL/dbt diffs only for low-risk, high-confidence categories (add a test, fix a cast, rewrite an inefficient query); validated by dry-run/EXPLAIN before display; approval workflow (approve/reject/edit); nothing auto-applied.
- FR-3.2 ETL efficiency: duplicated transformation logic; full-refresh where incremental is feasible.
- FR-3.3 Data contracts: definition + CI check before merge.

**Cross-cutting:** FR-X.1 track-record/confirmation rates in UI; FR-X.2 business-user trust indicator.

**Non-functional:** read-only by default; encryption in transit/at rest; SOC 2 readiness before enterprise GA (later ISO 27001); monitoring itself must be more reliable than what it monitors, and a failed check must itself be alertable; profiling must be cost-aware (sampling/incremental) so it doesn't inflate customer warehouse spend; every finding traceable to concrete evidence; immutable audit log of findings/fixes/approvals; adapter-based extensibility for new warehouses/orchestrators; no automated writes to customer production without explicit, scoped, revocable opt-in.

**Out of scope (v1):** autonomous fix execution; automated architecture redesign; non-warehouse sources (operational DBs, streaming).

**Key risks:** false positives / hallucinated matches or root causes eroding trust; integration breadth; LLM cost at scale.

### 2.4 Customer connection & security model
- **Two deployment models:** (1) Direct pull — SaaS control plane connects out to the customer warehouse with a scoped service account. Start here (design partners). (2) Agent-in-VPC — connector runs in the customer's cloud, sends only processed metadata outbound. Build later as the enterprise tier.
- **Core security story:** push computation *into* the warehouse via SQL; pull out only metadata and aggregates (null %, distinct counts, min/max as text). Raw values only if a customer explicitly enables sampling per table for semantic/PII features.
- **Access needed:** read-only role scoped to whitelisted DBs/schemas; INFORMATION_SCHEMA; query history/cost metadata; dbt manifest/logs; orchestrator run metadata. **Not needed:** write access, data exports, orchestrator execution environment.
- **Per warehouse:** Snowflake — dedicated role + key-pair auth (or OAuth), read on `SNOWFLAKE.ACCOUNT_USAGE`. BigQuery — service account with `bigquery.metadataViewer` + `bigquery.jobUser`, Workload Identity Federation. Databricks — service principal, Unity Catalog + system tables, OAuth.
- **Product security:** least privilege per asset, revocable in UI; secrets in a secrets manager (Vault / AWS Secrets Manager) with per-tenant keys, never in config/logs; strict tenant isolation (no shared embeddings/indexes across tenants); contractual no-training/no-retention with LLM provider; immutable exportable audit log.
- **Onboarding flow:** customer runs a setup script/Terraform you provide that creates the scoped role → pastes credentials (or deploys the agent) → initial read-only crawl → Phase 1 checks → nothing writes back unless they enable CI contract checks or approve individual fixes.
- **Honest caveat for sales:** in Snowflake, `SELECT` grants mean the role *could* read raw rows; "aggregates only" is enforced by our code, not by Snowflake permissions. Say this precisely to customers instead of "we can't see your data."

---

## 3. Tech stack

| Area | Choice |
|---|---|
| Language | **Python** (user's explicit choice) |
| Metadata store | **PostgreSQL** (no graph DB yet) |
| ORM | **SQLAlchemy** (2.x declarative) |
| Migrations | **Alembic** |
| Config | **pydantic-settings**, loaded from env / `.env`; `.env.example` documents all vars |
| Packaging | **plain pip + `pyproject.toml` (PEP 621)**; `pip-tools` later if lockfiles needed. **No Poetry.** Optional extras group `reasoning` for LLM deps |
| Testing | **pytest** + **testcontainers-postgres** |
| Lint/format/types | **ruff**, **mypy**, **pre-commit** |
| Task runner | **Makefile** (`install`, `test`, `lint`, `format`, `migrate`, `run-ingestion`) |
| CI | **GitHub Actions** (`.github/workflows/ci.yml`: lint + type-check + tests on PRs; testcontainers via Docker on runners) |
| Warehouse (first) | **Snowflake** (key-pair auth) |
| Transform layer | **dbt** (`dbt-snowflake`); reads `manifest.json` / `run_results.json` |
| SQL parsing / lineage | **sqlglot** |
| Alerting | **Slack incoming webhook** via `requests` (email/PagerDuty later via same interface) |
| API layer | FastAPI (placeholder package only, not built) |
| Profiling inspiration | Great Expectations / Soda Core (concepts; not adopted as deps) |
| Future | LLM API for semantic matching, root-cause agent (tool-calling loop over the graph), fix drafting; Airflow/Dagster adapters; BigQuery/Databricks adapters; Looker/Tableau metadata |
| Coding workflow | Planning assistant writes copilot prompts → AI coding copilot returns `implementation_plan.md` with open questions → assistant reviews/answers → copilot builds |

---

## 4. Decisions made (and why)

1. **Postgres + recursive CTEs instead of a graph DB.** Single-customer scale (thousands–low millions of nodes/edges) doesn't need Neo4j; Postgres gives transactions, tooling, hiring pool. Revisit only if traversal is measurably slow.
2. **Time-versioned nodes (`valid_from` / `valid_to`, insert-new-row, never UPDATE in place).** Free history; schema-drift detection = diff current vs previous row; feeds root cause ("what changed just before?"). `valid_to = NULL` = current.
3. **`Job` is append-only, one row per execution** — it *is* the run-history log.
4. **Edges are separate tables**, so edges like `DERIVES_FROM` can carry attributes (transform expression, producing job).
5. **One shared `Finding` table** for every detection/reasoning output (quality, drift, semantic, cost, architecture, root cause, fix). Deterministic findings have `confidence = NULL`; anything LLM-derived MUST have confidence + evidence.
6. **Deterministic vs LLM split.** Nulls, types, duplicates, schema diffs, lineage parsing, cost math are deterministic — never delegated to an LLM. LLMs only for semantic interpretation, root-cause narrative, fix drafting — always confidence-scored and human-reviewed.
7. **Metadata graph is the core IP**, not the LLM calls.
8. **Read-only first; execution never automatic.** Fixes are output as diffs/PRs.
9. **Warehouse-native compute** — profiling runs as SQL inside the warehouse.
10. **Start with one warehouse (Snowflake) + dbt.** Adapter pattern for the rest.
11. **Build order:** design the "how" in detail only for what is being built next. Root cause, fix generation, semantic matching are designed later once the graph and real incidents exist.
12. **src-layout** project structure with one subpackage per architecture layer.
13. **Detection check 2e (orphaned FK / referential integrity) deferred.** It needs live pushed-down warehouse queries (graph only stores aggregates), mixing two execution models and reintroducing warehouse-cost concerns. Will return as its own feature, designed together with cost optimization ("when is it OK to query the customer's warehouse?"). `BaseCheck` should not hard-code "graph-only forever."
14. **Health score weights (approved), subtracted from 100 per OPEN finding, floor 0:** QUALITY_ISSUE −20; SCHEMA_DRIFT −15; SEMANTIC_DUPLICATE −10; ARCHITECTURE_FLAG −10; COST_INEFFICIENCY −5; ROOT_CAUSE and RECOMMENDED_FIX 0 (diagnostic outputs, not problems). All weights configurable named constants.
15. **Health score aggregation:** schema and global scores = **average** of child asset scores (one minor table shouldn't tank a schema), **plus a stored `min_child_score`** on SCHEMA/GLOBAL snapshots so the future per-dashboard trust indicator can see the worst table even when the average looks fine.
16. **Alerting:** only on newly inserted findings; `alerted_at` on `Finding` for suppression (re-alert only after resolve + reopen); digest mode when > threshold (proposed 3) findings for the same asset in one batch; fallback default channel if no owner — never silently drop.
17. **Snowflake trial settings for validation:** "AI Data Cloud — For Enterprise" signup (not "Snowflake CoCo"); recommended **Standard** edition to stretch credits (user's final selection not confirmed); **AWS**; region **Asia Pacific (Thailand)** (closest available).
18. **Validate on Snowflake (not BigQuery)** because the existing ingestion adapter is Snowflake-specific.
19. **Validation dataset:** dbt Labs' `jaffle_shop` sample project + deliberately injected problems with a written answer key.

---

## 5. Current progress

### 5.1 Status summary
| Piece | Status |
|---|---|
| Product framing, competitive analysis, feasibility review | Done |
| SRS (`SRS_AI_Data_Engineer_Platform.md`) | Done (content summarized in §2.3) |
| Metadata graph schema (`models.py`), lineage queries, design README | [WRITTEN HERE] — full code below |
| Project scaffolding (src-layout, pyproject, alembic, ruff/mypy/pre-commit, Makefile, CI, testcontainers) | [COPILOT-BUILT, per user — UNVERIFIED in detail] |
| Snowflake + dbt ingestion adapter | Prompt written; copilot later treated it as existing — **[UNVERIFIED]**, confirm in repo |
| Deterministic detection engine (checks 1a–1d) | [COPILOT-BUILT, per user] |
| Alerting (Slack) + Health Score | [COPILOT-BUILT, per user: "done this"] |
| Validation against real data | **Not started.** Snowflake trial account just created |

### 5.2 Intended repo layout (from approved scaffolding plan)
```
pyproject.toml
.env.example
Makefile
.pre-commit-config.yaml
.github/workflows/ci.yml
alembic/
scripts/
docs/                      # architecture notes, SRS, detection.md, health_score.md, alerting.md
src/ai_data_engineer/
  config.py                # pydantic-settings Settings
  ingestion/
    snowflake/
    dbt/
    bigquery/              # placeholder
  graph/
    models.py              # schema (below, plus later additions)
    queries/               # lineage queries
  detection/
    rules/                 # base.py, null_rate.py, type_drift.py, schema_drift.py, distinct_count.py
    runner.py
  reasoning/               # placeholder (LLM phase)
  fixes/                   # placeholder
  api/                     # placeholder (FastAPI)
  alerting/                # notifier.py, slack_notifier.py, router.py
  health_score/            # calculator.py (pure), engine.py
  scheduler/
tests/                     # mirrors src/
```

### 5.3 Ingestion adapter — specified requirements
- **Snowflake connector:** key-pair auth; `INFORMATION_SCHEMA.TABLES` / `.COLUMNS` → `Asset` / `AssetColumn`; `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY` (fallback `INFORMATION_SCHEMA.QUERY_HISTORY`) → `Job` (query text, start/end, status, bytes scanned, rows produced, estimated credits); column stats (null rate, distinct count via `APPROX_COUNT_DISTINCT`, min/max as text) computed **inside Snowflake** — only aggregates leave.
- **dbt lineage parser:** `manifest.json` → model dependencies; compiled SQL → sqlglot column-level lineage → `DerivesFromEdge` with transform expression; `ProducesEdge`, `ReadsFromEdge`.
- **Idempotency:** match on natural key; if profile changed, close old row's `valid_to` and insert new version; never UPDATE.
- **Config:** credentials from env/config only.
- **Deliverables:** `snowflake_connector.py`, `dbt_lineage_parser.py`, `ingest.py`, README section (env vars + required grants), tests for idempotency and sqlglot lineage on sample SQL.
- **Constraints:** read-only, no raw values logged, connector/parser decoupled from Postgres writes (adapter pattern).

### 5.4 Detection engine — approved design
- `BaseCheck` abstract class (`run(session, asset_id)` → finding-shaped results) + simple list/dict registry.
- `runner.py`: `run_all_checks(session, asset_id=None)`; dedup — if an OPEN finding exists for the same asset/column/finding_type (+ signature), update `detected_at` and `evidence` instead of inserting; calls the alert router for newly inserted findings (clean, swappable call).
- Checks: **null_rate** (latest vs trailing N versions; z-score or absolute pp jump); **type_drift** (vs immediately prior version); **schema_drift** (separate "dropped" and "added" findings); **distinct_count** (ratio distinct/row_count drops sharply → possible duplicates).
- Findings: correct `finding_type`, `status=OPEN`, `confidence=NULL`, `evidence` JSONB with baseline, current value, threshold; titles/descriptions quote actual numbers and dates.
- Config (`config.py`): `DETECTION_NULL_RATE_ZSCORE_THRESHOLD=3.0`, `DETECTION_NULL_RATE_ABS_THRESHOLD=0.10`, `DETECTION_DISTINCT_DROP_THRESHOLD=0.15`, `DETECTION_TRAILING_VERSIONS=7`.
- Tests: per-rule positive/negative/boundary (in-memory); testcontainers integration; dedup test (run twice → no duplicates). `docs/detection.md`.

### 5.5 Alerting + Health Score — approved design
- **Schema changes:** `Finding.alerted_at: datetime | None`; new `HealthScoreSnapshot` (`asset_id` nullable, `scope` enum ASSET/SCHEMA/GLOBAL, `score`, `min_child_score` for SCHEMA/GLOBAL, `snapshot_time`) — via Alembic migration.
- **Alerting:** `BaseNotifier` (`send_message()`, `send_digest()`), `SlackNotifier` (webhook URL from config, `requests`), `AlertRouter(notifier)` resolves owner via `OwnsEdge` (fallback default channel), groups by asset, digest if count > threshold, sets `alerted_at`. Messages: title, description, asset name/reference, clearly from the platform.
- **Health score:** `calculator.py` pure function `max(0, 100 − sum(weights))`; `engine.py` `compute_health_scores(session)` writes ASSET snapshots then SCHEMA/GLOBAL averages + min_child_score; runs as its own step (frequency documented in `docs/health_score.md`).
- Config: Slack webhook URL, digest threshold, fallback channel, severity weights.
- Tests: pure formula tests; router owner-resolution/suppression/digest; integration with faked Slack (no real calls); snapshot integration test. Docs: `docs/health_score.md`, `docs/alerting.md`.
- Documented known limitation: severity is per finding_type only, not scaled by magnitude in `evidence`.

### 5.6 Code written in the original conversation (full)

> NOTE: In the repo, this file has since moved to `src/ai_data_engineer/graph/models.py` and was extended by the copilot with `Finding.alerted_at` and a `HealthScoreSnapshot` model (see §5.5). The version below is the original.

#### `models.py`
```python
"""
Metadata Graph — core schema.

Design principles:
- Nodes and edges are plain Postgres tables (no graph DB needed at this scale).
- Every node is time-versioned (valid_from/valid_to) so schema drift and
  history are queryable for free — this is what root-cause investigation
  and schema-drift detection both read from.
- Edges reference nodes by stable internal id, not by name — names change
  (renames), ids don't.
- source_system + source_id together are the natural key from the
  customer's actual warehouse (e.g. Snowflake database.schema.table),
  kept separately from our internal id so re-ingestion is idempotent.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column as SAColumn,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Shared versioning mixin
# ---------------------------------------------------------------------------


class Versioned:
    """
    Every node uses this pattern instead of UPDATE-in-place:
    a new row is inserted with a new valid_from, and the previous row's
    valid_to is closed. This gives full history with no separate audit table.
    valid_to = NULL means "currently active".
    """

    valid_from: datetime = SAColumn(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    valid_to: datetime | None = SAColumn(DateTime(timezone=True), nullable=True)
    ingested_at: datetime = SAColumn(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )


class SourceSystem(str, enum.Enum):
    SNOWFLAKE = "snowflake"
    BIGQUERY = "bigquery"
    DATABRICKS = "databricks"
    DBT = "dbt"
    AIRFLOW = "airflow"
    LOOKER = "looker"
    TABLEAU = "tableau"


# ---------------------------------------------------------------------------
# Node: Asset (a table or view in the warehouse)
# ---------------------------------------------------------------------------


class Asset(Versioned, Base):
    """
    A physical or logical dataset: a table, view, or materialized dbt model.
    """

    __tablename__ = "asset"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)

    # Natural key from the source system — used to match re-ingested rows
    # to the same logical asset across versions.
    source_system = SAColumn(Enum(SourceSystem), nullable=False)
    database_name = SAColumn(String, nullable=False)
    schema_name = SAColumn(String, nullable=False)
    table_name = SAColumn(String, nullable=False)

    asset_type = SAColumn(String, nullable=False)  # "table" | "view" | "materialized_view"
    row_count_estimate = SAColumn(Numeric, nullable=True)
    size_bytes_estimate = SAColumn(Numeric, nullable=True)

    # Free-form warehouse-specific metadata (clustering keys, partitioning,
    # retention settings) — kept as JSONB rather than a rigid column set
    # because this varies a lot by warehouse.
    physical_properties = SAColumn(JSONB, nullable=True)

    is_deleted = SAColumn(Boolean, nullable=False, default=False)

    columns = relationship("AssetColumn", back_populates="asset")

    __table_args__ = (
        Index(
            "ix_asset_natural_key_active",
            "source_system",
            "database_name",
            "schema_name",
            "table_name",
            unique=False,  # not unique because history keeps old rows
        ),
    )


# ---------------------------------------------------------------------------
# Node: AssetColumn
# ---------------------------------------------------------------------------


class AssetColumn(Versioned, Base):
    """
    A column within an Asset. Carries the statistical profile snapshot
    (null rate, distinct count, etc.) used by the deterministic detection
    layer — recomputed on each profiling run, versioned like everything else.
    """

    __tablename__ = "asset_column"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    asset_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset.id"), nullable=False)

    column_name = SAColumn(String, nullable=False)
    data_type = SAColumn(String, nullable=False)
    ordinal_position = SAColumn(Numeric, nullable=True)
    is_nullable = SAColumn(Boolean, nullable=True)

    # Statistical profile snapshot — this is what quality checks diff
    # against the previous version's row to detect drift/anomalies.
    null_rate = SAColumn(Numeric, nullable=True)
    distinct_count = SAColumn(Numeric, nullable=True)
    min_value_repr = SAColumn(String, nullable=True)  # stored as text repr, not raw value
    max_value_repr = SAColumn(String, nullable=True)

    is_pii_flagged = SAColumn(Boolean, nullable=False, default=False)
    pii_confidence = SAColumn(Numeric, nullable=True)  # 0-1, from PII detector

    asset = relationship("Asset", back_populates="columns")

    __table_args__ = (Index("ix_asset_column_asset_id", "asset_id"),)


# ---------------------------------------------------------------------------
# Node: Job (a dbt model run, Airflow task, or any transformation execution)
# ---------------------------------------------------------------------------


class JobStatus(str, enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    RUNNING = "running"
    SKIPPED = "skipped"


class Job(Base):
    """
    A single execution of a transformation (a dbt model run, an Airflow
    task instance, a Spark job). NOT versioned like other nodes — each
    execution is its own row; this table IS the run-history log that
    root-cause investigation queries.
    """

    __tablename__ = "job"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)

    source_system = SAColumn(Enum(SourceSystem), nullable=False)
    job_name = SAColumn(String, nullable=False)  # e.g. dbt model name, DAG task id

    started_at = SAColumn(DateTime(timezone=True), nullable=False)
    finished_at = SAColumn(DateTime(timezone=True), nullable=True)
    status = SAColumn(Enum(JobStatus), nullable=False)

    rows_affected = SAColumn(Numeric, nullable=True)
    bytes_scanned = SAColumn(Numeric, nullable=True)
    cost_estimate = SAColumn(Numeric, nullable=True)  # for cost-optimization features

    # The actual SQL or compiled dbt code for this run — needed by both
    # the root-cause agent (diffing against prior runs) and fix generation
    # (as context for drafting a patch).
    executed_sql = SAColumn(Text, nullable=True)
    error_message = SAColumn(Text, nullable=True)

    __table_args__ = (Index("ix_job_name_started", "job_name", "started_at"),)


# ---------------------------------------------------------------------------
# Node: Dashboard
# ---------------------------------------------------------------------------


class Dashboard(Base):
    __tablename__ = "dashboard"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    source_system = SAColumn(Enum(SourceSystem), nullable=False)
    dashboard_name = SAColumn(String, nullable=False)
    url = SAColumn(String, nullable=True)


# ---------------------------------------------------------------------------
# Node: Owner (person or team responsible for an asset/job)
# ---------------------------------------------------------------------------


class Owner(Base):
    __tablename__ = "owner"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    name = SAColumn(String, nullable=False)
    email = SAColumn(String, nullable=True)
    slack_channel = SAColumn(String, nullable=True)
    team = SAColumn(String, nullable=True)


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------
# Edges are their own tables rather than foreign keys embedded in nodes,
# because several edge types are many-to-many and some (DERIVES_FROM)
# need their own attributes (e.g. the transformation expression).


class ProducesEdge(Base):
    """Job -> Asset : this job writes/creates this asset."""

    __tablename__ = "edge_produces"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    job_id = SAColumn(UUID(as_uuid=True), ForeignKey("job.id"), nullable=False)
    asset_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset.id"), nullable=False)

    __table_args__ = (UniqueConstraint("job_id", "asset_id"),)


class ReadsFromEdge(Base):
    """Job -> Asset : this job reads this asset as input."""

    __tablename__ = "edge_reads_from"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    job_id = SAColumn(UUID(as_uuid=True), ForeignKey("job.id"), nullable=False)
    asset_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset.id"), nullable=False)

    __table_args__ = (UniqueConstraint("job_id", "asset_id"),)


class DerivesFromEdge(Base):
    """
    Column -> Column : column-level lineage. This is what powers
    "which downstream columns are affected if this column breaks" —
    the core of impact analysis.
    """

    __tablename__ = "edge_derives_from"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    source_column_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset_column.id"), nullable=False)
    target_column_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset_column.id"), nullable=False)

    # e.g. "direct copy", "SUM(amount)", "CASE WHEN ... " — the transform
    # expression, extracted from the parsed SQL. Useful context for the
    # root-cause agent and for semantic-duplicate detection.
    transform_expression = SAColumn(Text, nullable=True)
    produced_by_job_id = SAColumn(UUID(as_uuid=True), ForeignKey("job.id"), nullable=True)

    __table_args__ = (
        UniqueConstraint("source_column_id", "target_column_id", "produced_by_job_id"),
    )


class FeedsEdge(Base):
    """Asset -> Dashboard : this asset is used by this dashboard."""

    __tablename__ = "edge_feeds"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    asset_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset.id"), nullable=False)
    dashboard_id = SAColumn(UUID(as_uuid=True), ForeignKey("dashboard.id"), nullable=False)

    __table_args__ = (UniqueConstraint("asset_id", "dashboard_id"),)


class OwnsEdge(Base):
    """Owner -> Asset (or Job) : ownership, for alert routing."""

    __tablename__ = "edge_owns"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    owner_id = SAColumn(UUID(as_uuid=True), ForeignKey("owner.id"), nullable=False)
    asset_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset.id"), nullable=True)
    job_id = SAColumn(UUID(as_uuid=True), ForeignKey("job.id"), nullable=True)


# ---------------------------------------------------------------------------
# Findings — where detection/reasoning layers write their output.
# Everything in Phases 1-3 (quality issues, root causes, recommendations)
# lands here, so the UI/alerting/health-score layers all read one table.
# ---------------------------------------------------------------------------


class FindingType(str, enum.Enum):
    QUALITY_ISSUE = "quality_issue"  # null spike, type drift, duplicate, etc.
    SCHEMA_DRIFT = "schema_drift"
    SEMANTIC_DUPLICATE = "semantic_duplicate"
    COST_INEFFICIENCY = "cost_inefficiency"
    ARCHITECTURE_FLAG = "architecture_flag"
    ROOT_CAUSE = "root_cause"
    RECOMMENDED_FIX = "recommended_fix"


class FindingStatus(str, enum.Enum):
    OPEN = "open"
    CONFIRMED = "confirmed"  # human confirmed this is real/correct
    REJECTED = "rejected"  # human said this is wrong
    RESOLVED = "resolved"
    APPLIED = "applied"  # for fixes: the human approved and applied it


class Finding(Base):
    __tablename__ = "finding"

    id = SAColumn(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    finding_type = SAColumn(Enum(FindingType), nullable=False)
    status = SAColumn(Enum(FindingStatus), nullable=False, default=FindingStatus.OPEN)

    # What this finding is about — nullable FKs because a finding can be
    # asset-level, column-level, or job-level.
    asset_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset.id"), nullable=True)
    column_id = SAColumn(UUID(as_uuid=True), ForeignKey("asset_column.id"), nullable=True)
    job_id = SAColumn(UUID(as_uuid=True), ForeignKey("job.id"), nullable=True)

    title = SAColumn(String, nullable=False)
    description = SAColumn(Text, nullable=False)

    # Every non-deterministic finding (semantic, root cause, fix) MUST
    # carry a confidence score and the evidence it was based on —
    # this is what feeds the FR-X.1 trust/track-record layer.
    confidence = SAColumn(
        Numeric, nullable=True
    )  # 0-1; null for deterministic findings (they're exact)
    evidence = SAColumn(JSONB, nullable=True)  # links/refs to jobs, diffs, log lines, queries used

    # For RECOMMENDED_FIX findings specifically:
    proposed_sql_diff = SAColumn(Text, nullable=True)
    dry_run_validated = SAColumn(Boolean, nullable=True)

    estimated_cost_savings = SAColumn(Numeric, nullable=True)

    detected_at = SAColumn(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    resolved_at = SAColumn(DateTime(timezone=True), nullable=True)
    reviewed_by_owner_id = SAColumn(UUID(as_uuid=True), ForeignKey("owner.id"), nullable=True)

    __table_args__ = (
        Index("ix_finding_asset_id", "asset_id"),
        Index("ix_finding_type_status", "finding_type", "status"),
    )
```

#### `lineage_queries.py`
```python
"""
Lineage traversal — upstream (root-cause) and downstream (impact analysis).

Both use a recursive CTE over edge_derives_from (column-level lineage).
This is the single most-used query pattern in the whole platform, so it's
worth getting right and fast (indexed) before anything else.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session


UPSTREAM_QUERY = text("""
-- Given a column, find everything that feeds into it, transitively.
-- Used by: root-cause investigation ("what could have caused this?")
WITH RECURSIVE upstream AS (
    SELECT
        source_column_id,
        target_column_id,
        produced_by_job_id,
        1 AS depth
    FROM edge_derives_from
    WHERE target_column_id = :column_id

    UNION ALL

    SELECT
        e.source_column_id,
        e.target_column_id,
        e.produced_by_job_id,
        u.depth + 1
    FROM edge_derives_from e
    JOIN upstream u ON e.target_column_id = u.source_column_id
    WHERE u.depth < :max_depth  -- guard against cycles / runaway traversal
)
SELECT DISTINCT
    ac.id AS column_id,
    ac.column_name,
    a.database_name,
    a.schema_name,
    a.table_name,
    u.produced_by_job_id,
    u.depth
FROM upstream u
JOIN asset_column ac ON ac.id = u.source_column_id
JOIN asset a ON a.id = ac.asset_id
ORDER BY u.depth;
""")


DOWNSTREAM_QUERY = text("""
-- Given a column, find everything that depends on it, transitively.
-- Used by: impact analysis ("what breaks if this is wrong?")
WITH RECURSIVE downstream AS (
    SELECT
        source_column_id,
        target_column_id,
        produced_by_job_id,
        1 AS depth
    FROM edge_derives_from
    WHERE source_column_id = :column_id

    UNION ALL

    SELECT
        e.source_column_id,
        e.target_column_id,
        e.produced_by_job_id,
        d.depth + 1
    FROM edge_derives_from e
    JOIN downstream d ON e.source_column_id = d.target_column_id
    WHERE d.depth < :max_depth
)
SELECT DISTINCT
    ac.id AS column_id,
    ac.column_name,
    a.database_name,
    a.schema_name,
    a.table_name,
    d.produced_by_job_id,
    d.depth
FROM downstream d
JOIN asset_column ac ON ac.id = d.target_column_id
JOIN asset a ON a.id = ac.asset_id
ORDER BY d.depth;
""")


DOWNSTREAM_DASHBOARDS_QUERY = text("""
-- Extend impact analysis all the way to dashboards, via the downstream
-- assets found above. Run this second, feeding in the asset_ids from
-- the downstream column query.
SELECT DISTINCT
    d.id AS dashboard_id,
    d.dashboard_name,
    d.url
FROM edge_feeds f
JOIN dashboard d ON d.id = f.dashboard_id
WHERE f.asset_id = ANY(:asset_ids);
""")


def get_upstream_lineage(session: Session, column_id: str, max_depth: int = 10):
    """Root-cause investigation entry point: trace a problem backward."""
    return (
        session.execute(UPSTREAM_QUERY, {"column_id": column_id, "max_depth": max_depth})
        .mappings()
        .all()
    )


def get_downstream_lineage(session: Session, column_id: str, max_depth: int = 10):
    """Impact analysis entry point: trace a problem forward."""
    return (
        session.execute(DOWNSTREAM_QUERY, {"column_id": column_id, "max_depth": max_depth})
        .mappings()
        .all()
    )


def get_affected_dashboards(session: Session, asset_ids: list[str]):
    return session.execute(DOWNSTREAM_DASHBOARDS_QUERY, {"asset_ids": asset_ids}).mappings().all()
```

#### Metadata graph README (key points)
- Postgres not graph DB (scale doesn't need it).
- Every node time-versioned; schema drift = diff vs last row.
- `Job` append-only per execution = run-history log.
- Edges as tables so `DERIVES_FROM` carries transform expression + producing job.
- `Finding` single output table; deterministic → `confidence NULL`; LLM-derived → confidence + evidence mandatory.
- Not in schema by design: raw values (`min/max_value_repr` are text reprs), data contracts (later), **multi-tenancy (`tenant_id` + Postgres RLS or schema-per-tenant) — required before any real customer.**

### 5.7 Snowflake validation environment (in progress)
- Trial account created ("AI Data Cloud — For Enterprise" signup, AWS, Asia Pacific (Thailand)). Edition: Standard recommended — confirm which was chosen.
- **Remaining setup steps:**
  1. Log in; note account identifier/URL.
  2. Get dbt Labs' `jaffle_shop` sample project (github.com/dbt-labs/jaffle_shop — if that repo is archived, use dbt Labs' newer `jaffle-shop` repo). `pip install dbt-snowflake`.
  3. Configure `profiles.yml` (password auth is fine just for loading sample data); `dbt seed` then `dbt run` → real tables, real `manifest.json`, real query history.
  4. **Inject known problems and write an answer key**, e.g.: `UPDATE orders SET customer_id = NULL WHERE id % 50 = 0` (null spike); insert a batch of duplicate rows (distinct-count check); change/widen a column type (type drift); drop a column from one dbt model between runs (schema drift). **Note:** drift/spike checks compare versions — ingest once *before* injecting, then again *after*.
  5. Create the scoped read-only role + key-pair service user (SQL below), verify, run smoke test.

#### Key pair generation
```bash
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out aide_rsa_key.p8 -nocrypt
openssl rsa -in aide_rsa_key.p8 -pubout -out aide_rsa_key.pub
```
Never commit `*.p8` (add to `.gitignore`). Paste the public key body (between BEGIN/END lines, no line breaks) into the SQL below. Use a passphrase for anything beyond a throwaway trial.

#### Role / warehouse / user setup (Snowsight; replace `JAFFLE_DB`)
```sql
USE ROLE ACCOUNTADMIN;

CREATE ROLE IF NOT EXISTS AIDE_READONLY;

CREATE WAREHOUSE IF NOT EXISTS AIDE_WH
  WAREHOUSE_SIZE = 'XSMALL'
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE;
GRANT USAGE ON WAREHOUSE AIDE_WH TO ROLE AIDE_READONLY;

GRANT USAGE ON DATABASE JAFFLE_DB TO ROLE AIDE_READONLY;
GRANT USAGE ON ALL SCHEMAS IN DATABASE JAFFLE_DB TO ROLE AIDE_READONLY;
GRANT USAGE ON FUTURE SCHEMAS IN DATABASE JAFFLE_DB TO ROLE AIDE_READONLY;
GRANT SELECT ON ALL TABLES IN DATABASE JAFFLE_DB TO ROLE AIDE_READONLY;
GRANT SELECT ON FUTURE TABLES IN DATABASE JAFFLE_DB TO ROLE AIDE_READONLY;
GRANT SELECT ON ALL VIEWS IN DATABASE JAFFLE_DB TO ROLE AIDE_READONLY;
GRANT SELECT ON FUTURE VIEWS IN DATABASE JAFFLE_DB TO ROLE AIDE_READONLY;

-- Query history / usage metadata (tighter than IMPORTED PRIVILEGES)
GRANT DATABASE ROLE SNOWFLAKE.USAGE_VIEWER TO ROLE AIDE_READONLY;
-- Fallback if something needed isn't visible:
-- GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE AIDE_READONLY;

CREATE USER IF NOT EXISTS AIDE_SVC
  TYPE = SERVICE
  DEFAULT_ROLE = AIDE_READONLY
  DEFAULT_WAREHOUSE = AIDE_WH
  RSA_PUBLIC_KEY = 'PASTE_PUBLIC_KEY_HERE';
GRANT ROLE AIDE_READONLY TO USER AIDE_SVC;
```

#### Verification
```sql
USE ROLE AIDE_READONLY;
USE WAREHOUSE AIDE_WH;
SELECT COUNT(*) FROM JAFFLE_DB.<your_schema>.ORDERS;            -- should work
SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY LIMIT 5;      -- should work (may lag)
CREATE TABLE JAFFLE_DB.PUBLIC.should_fail (a INT);                -- should FAIL
```
`ACCOUNT_USAGE.QUERY_HISTORY` can lag up to ~45 minutes; `INFORMATION_SCHEMA.QUERY_HISTORY()` is real-time.

#### Smoke-test script spec (`scripts/smoke_test_snowflake.py`, to be built by copilot)
Using only key-pair env credentials (account, `AIDE_SVC`, private key path, `AIDE_READONLY`, `AIDE_WH`): (1) print `current_user()`, `current_role()`, `current_warehouse()`; (2) list visible DBs/schemas/tables; (3) one `INFORMATION_SCHEMA.COLUMNS` query, print row count; (4) read 5 rows from `ACCOUNT_USAGE.QUERY_HISTORY` (empty due to latency ≠ failure); (5) attempt `CREATE TABLE`, expect rejection, print "read-only enforcement OK". Never print key or raw values; exit non-zero on failure (except query-history latency); add env vars to `.env.example`.

---

## 6. Problems and open questions

### Design issues spotted while writing this brief (not yet discussed — review early)
1. **Edges point at versioned node IDs.** Each new version of an `AssetColumn`/`Asset` gets a new UUID, but `DerivesFromEdge`, `ProducesEdge`, `ReadsFromEdge`, `FeedsEdge`, `OwnsEdge`, and `Finding.column_id` reference those row IDs. After re-ingestion, lineage/ownership edges point to *old* versions, and lineage queries joining on `asset_column.id` may miss current rows or need edge duplication per version. Likely fix: a stable logical ID per asset/column (e.g. `asset_key` / `column_key`) that edges reference, separate from the version row ID. **Highest-priority schema question — resolve before ingesting real data repeatedly.**
2. **`Job` mixes "definition" and "execution."** Lineage from the dbt manifest is about a *model* (stable), while `Job` rows are *runs*. `ProducesEdge`/`ReadsFromEdge` keyed on run IDs means edges per run. Consider a stable `JobDefinition`/model node with runs referencing it.
3. **No source for ownership data.** `OwnsEdge` drives alert routing, but nothing ingests owners yet (dbt `meta.owner`, a config file, or a UI?). Currently alerts will all hit the fallback channel.
4. **No `tenant_id` / multi-tenancy** — required before any real customer.
5. **Dashboards/FeedsEdge not ingested** — no BI connector yet, so impact analysis stops at tables.
6. Minor: `datetime.utcnow` is deprecated in Python 3.12+ (prefer `datetime.now(timezone.utc)`).

### Known open items from the conversation
7. **Ingestion adapter status unverified** — confirm it exists and works.
8. **Alert flood on first run** — every finding looks new on first ingestion of a messy warehouse. Probably need a "quiet first run / baseline mode" before alerting goes live. Not built.
9. **Null-rate check cold start** — z-score needs ~7 versions of history; ensure new assets with 1–2 versions are handled gracefully (no crash, no nonsense z-score). Needs code review.
10. **Severity not scaled by magnitude** — every QUALITY_ISSUE costs −20 regardless of size (documented v1 limitation).
11. **Threshold defaults untuned** — expect first-run false positives; tune `config.py` after auditing real findings.
12. **Check 2e (orphaned FK / referential integrity) deferred** — will need a pushed-down-query design shared with cost optimization.
13. **testcontainers in GitHub Actions** — copilot was asked to verify CI actually starts Postgres; confirm.
14. **Health score validity** — weights not yet checked against human intuition on real tables.
15. **No design partner yet** — validation is on a Snowflake trial + jaffle_shop; a real company's messy warehouse is still needed later.
16. **Team/skills unknown** — user says "we"; team size not stated. Highest-skill piece is lineage parsing, not the LLM parts.
17. **Not yet decided:** LLM provider/orchestration approach (raw API vs agent framework) for root cause; scheduler choice (own scheduler vs Temporal/Airflow-as-library); BigQuery/Databricks timing; UI/frontend stack; deployment/hosting; pricing.

---

## 7. Constraints and preferences

- **Language: Python.**
- **Human approval before any execution**; read-only against customer systems by default.
- **No raw data leaves the warehouse** for core features; aggregates/metadata only.
- **No LLM calls in deterministic layers** (detection, lineage parsing, cost math) — hard constraint.
- **No hardcoded secrets** anywhere; everything via `config.py`/env; `.env.example` documents vars.
- **No real external calls in tests** (Slack etc. mocked).
- **Thresholds/weights configurable** named constants with documented defaults; no magic numbers.
- **Transparent, explainable outputs** — findings quote actual numbers; health score formula documented for customers.
- **Small, independent, testable units** — no monolithic "run everything" functions.
- **Avoid over-engineering** — simple registries over plugin systems; Postgres over graph DB; pip over Poetry.
- **Budget:** effectively zero so far — Snowflake free trial (limited credits/days), XSMALL warehouse with 60s auto-suspend to conserve credits.
- **No deadline stated.**
- **Process preference:** plan in detail only the next step; validate before building further; get real data before Phase 2.
- **Docs:** user prefers Markdown (`.md`) documents.
- **Workflow:** assistant writes copilot prompts; copilot proposes `implementation_plan.md` with open questions; assistant reviews and answers; copilot implements.

---

## 8. Next steps (in order)

1. **Resync with the real code.** Paste the current versions of: `src/ai_data_engineer/graph/models.py`, `config.py`, ingestion package, `detection/` (rules + runner), `alerting/`, `health_score/`, `pyproject.toml`, `Makefile`, and the Alembic migrations. Confirm what actually exists vs this brief (especially the ingestion adapter).
2. **Run the local sanity checks:** `make migrate` on a fresh Postgres (all tables incl. `finding.alerted_at` and `health_score_snapshot` created), `make test`, `make lint`; confirm CI passes with testcontainers.
3. **Decide on schema issue #1 (stable logical IDs for edges) and #2 (job definition vs run)** before ingesting real data repeatedly — cheaper to fix now than after history accumulates.
4. **Finish the Snowflake validation environment** (§5.7): jaffle_shop → `dbt seed` / `dbt run` → create role + key-pair user → verify → build and run the smoke test.
5. **Ingest once (baseline), then inject known problems and record the answer key, then ingest again.**
6. **Validate (Layer 2 — real data, human judgment):**
   - Inspect raw graph: do assets/columns/types/row counts match reality?
   - Manually verify lineage on 5–10 tables you understand.
   - Audit every finding: caught all injected problems? any false positives? → tune thresholds.
   - Watch Slack: flood on first run? → build quiet-baseline mode if so.
   - Sanity-check health scores against human intuition → adjust weights.
   - Keep a running log of everything wrong = Phase 1.5 backlog; clear most of it before Phase 2.
7. **Then Phase 2:** cost optimization first (deterministic, high ROI, demoable), root-cause investigation next (the real differentiator; build the FR-X.1 track-record layer alongside it). Design referential-integrity checks (deferred 2e) with cost optimization.
8. **In parallel when possible:** find 1–3 design partners (even friendly/free) with real Snowflake + dbt access; plan `tenant_id`/RLS before any real customer.

### Success metric to carry forward
Phase 1: on the test warehouse, detection catches **exactly** the injected problems (no misses, explainable false positives only) and lineage matches known relationships.
Go/no-go for Phase 2 root cause: *can it correctly identify the root cause of 3 real historical incidents whose answer is already known, using only lineage + logs + schema history?*
