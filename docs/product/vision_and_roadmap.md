# AI Data Engineer: Vision, Design & Roadmap

*Version 1, 2026-10-04. Detailed designs: [metadata store](../design/metadata_store.md) (1.1), [test lab](../design/test_lab.md) (1.2), [Postgres adapter](../design/postgres_adapter.md) (1.3), [relationship discovery](../design/relationship_discovery.md) (1.4). Written after the scope change from "Snowflake + dbt monitor" to "database-agnostic anomaly engine". This is the reference for **what** we build and **why**. [`CLAUDE.md`](../../CLAUDE.md) tracks **where we are**.*

---

## 1. The problem

Every company that runs software has data that has quietly gone wrong:

- **Transactional databases behind applications** (Postgres, MySQL, SQL Server, Oracle) have grown for years. Foreign keys were never declared, or were named `cust_no` in one table, `CUSTID` in another and `client_ref` in a third. Nobody fully knows what connects to what.
- **Daily ETL jobs** copy data between systems and warehouses. When one half-fails, tables go stale or half-empty, and no one notices until a report is wrong.
- **Bad values pile up**: nulls where there should be none, `"USA"` / `"US"` / `"United States"` side by side, ship dates before order dates, totals that don't match their line items, orders pointing to customers that don't exist.
- **The people who understood the schema have left.** Documentation is missing or wrong.

Existing tools fall into three groups, and none covers this:
- **Data observability tools** (Monte Carlo, Metaplane, Bigeye, Anomalo, …) mostly target **clean cloud warehouses with dbt**. They assume tidy metadata.
- **Data quality frameworks** (Great Expectations, Soda, dbt tests) need **humans to write every rule**, which nobody has time for on a legacy database with 800 tables.
- **Database monitoring** (Datadog, pganalyze, …) watches **performance**, not whether the data inside is correct.

## 2. What we're building

**A database-agnostic, AI-assisted anomaly detection engine.** You point it at any database, transactional or analytical, however messy. It:

1. **Discovers** the structure and the *real* relationships, including undeclared and misnamed foreign keys.
2. **Learns what "normal" looks like** for every table and column from the data's own history.
3. **Finds the widest possible range of anomalies** (section 3), each backed by concrete evidence.
4. **Proposes business rules** with AI. A human approves or rejects each one, and approved rules then run as plain SQL forever.
5. **Explains** what an anomaly affects (connected tables) and, later, **why** it happened. It proposes fixes for **human approval**; it never executes them on its own.
6. **Documents the database automatically**: a data dictionary and a relationship map, including inferred relationships.
7. **Gets smarter from feedback**. Every confirmed or rejected finding tunes it, and that track record is visible to the user.

> **Pitch:** Point it at any database, however messy. It tells you what's wrong, what it breaks and why, with evidence, and it gets smarter every time you confirm or reject a finding.

### Who it's for
| User | What they get |
|---|---|
| Backend / data engineer maintaining a legacy DB | A map of the database, hidden relationships, integrity problems |
| Data / analytics engineer running ETLs | Freshness, volume and quality alerts before reports break |
| Engineering / data lead | A health score, trends, and evidence to justify cleanup work |
| Analysts / business users (later) | "Can I trust this table right now?" |
| Security / compliance (later) | PII found in unexpected places |

---

## 3. Anomaly taxonomy: what "maximum kinds" means

| # | Category | Examples | How we detect it | Stage |
|---|---|---|---|---|
| 1 | **Structural** | Table without a primary key; FK column without an index; same concept stored as `INT` here and `VARCHAR` there; column dropped or retyped (schema drift) | Rules over metadata, plus diffs between metadata versions | 1.5 |
| 2 | **Relational / integrity** | Undeclared or misnamed FKs; orphan rows (an order whose customer doesn't exist); cardinality change (a 1-to-1 that became 1-to-many); duplicate entities | Relationship discovery (§5), then orphan / cardinality / duplicate checks | 1.4 |
| 3 | **Column values** | Null spikes; invalid formats (emails, phones, postcodes); out-of-range or impossible values (negative quantity, birth date in 2090); inconsistent categories | In-DB profiling, learned baselines, inferred patterns | 1.5 |
| 4 | **Business rules** | `ship_date ≥ order_date`; `order.total = SUM(order_items.amount)`; status never goes `cancelled → shipped`; balance = sum of transactions | **AI proposes → human approves → compiled to SQL** | 2 |
| 5 | **Time series** | Row count dropped 60%; table not updated at its usual 02:00; failed or partial load; distribution shift | Statistics on the measurement history, seasonality-aware | 1.5 |
| 6 | **Row-level outliers** | A transaction 100× this customer's normal; a burst of unusual activity | ML (e.g. isolation forest) on features computed in-DB | 2 |
| 7 | **Cross-system** | Source DB and warehouse copy disagree after ETL (counts, sums, key sets) | Reconciliation across two adapters | 3 |
| 8 | **Semantic** | PII in unexpected columns; the same meaning under different names | Heuristics + AI, with confidence | 2–3 |
| 9 | **DB health** *(add-on)* | Slow queries, table bloat, lock contention, unused indexes | System catalogs / stats views | 3 |

---

## 4. Who defines the rules?

No fixed numbers about anyone's data are hardcoded. A 30% null rate is normal for `middle_name` and a disaster for `order_id`. Rules come from five places:

| Source | Defines | Example |
|---|---|---|
| **Our code** | The *kinds* of checks | "Compare null rate with its own history"; "a table should have a primary key" |
| **Configuration** | How sensitive each check is: a default, overridable per tenant / source / table / column | Null-rate alert at 3 standard deviations; ignore `audit_log` |
| **The data itself** | What "normal" is, learned per column and table | `orders.customer_id` is 0.00–0.1% null; `orders` grows by 9–11k rows a day |
| **AI** | *Proposals* for relationships and business rules | "`invoices.CUSTID` → `customers.id`, confidence 0.94 (evidence: names, 99.7% value overlap, 312 joins in query logs)" |
| **Humans** | Approve or reject proposals and findings, add their own expectations | Approve the rule above; mark a finding "expected, not a problem" |

**Principle: AI discovers, humans approve, the engine enforces.** At check time everything is deterministic SQL and statistics: repeatable, cheap and explainable. AI is never in the loop that decides whether a check passes.

### How "learning normal" works (example)
Each run records a measurement, for example the null rate of `orders.customer_id`:

```
run:        1     2     3     4     5     6     7     8
null_rate:  0.0%  0.0%  0.1%  0.0%  0.0%  0.1%  0.0%  2.0%   ← new
```

The baseline is about 0.03% with very little spread, so 2.0% is extremely unusual and becomes a finding: *"null rate of orders.customer_id rose from a 7-run average of 0.03% to 2.0%."* A column that is normally 28–32% null and comes in at 31% is **not** flagged. The code is the same; the result differs because each column is compared with itself.

**Cold start:** with only one or two measurements there is no "normal" yet. Checks must report *"insufficient history"* instead of guessing, and the **first run sends no alerts** (quiet baseline mode). Otherwise a messy database would produce hundreds of alerts on day one.

---

## 5. Relationship discovery (the core of messy databases)

Most legacy databases declare few or no foreign keys. We infer them from evidence, mostly **without AI**:

| Signal | Example | Strength | Cost |
|---|---|---|---|
| **Declared FKs** | `REFERENCES customers(id)` | Certain | Free (metadata) |
| **Query-log joins** | Apps and analysts run `JOIN customers c ON o.cust_no = c.id` thousands of times (`pg_stat_statements`, query history) | Very strong; people already know it | Free (logs + sqlglot parsing) |
| **Name similarity** | `cust_no`, `CUSTID` and `customer_id` normalize to "customer + id" | Weak alone; good for finding candidates | Free |
| **Type compatibility** | Both integer, or both UUID | A filter | Free |
| **Value inclusion** | 99.8% of `orders.cust_no` values exist in `customers.id`, and `customers.id` is unique | Strong confirmation | In-DB query; run on candidates only |
| **AI review** | `kunde_nr` vs `client_ref` | For ambiguous cases | Confidence + human confirmation |

Each relationship is stored with its **sources, confidence, evidence and review status** (proposed / confirmed / rejected). Composite (multi-column) keys are supported, since they're common in legacy schemas. Confirmed relationships then power:
- orphan checks, cardinality checks and duplicate detection
- impact analysis ("what is connected to this broken table?")
- the auto-generated relationship map and data dictionary

---

## 6. Architecture

```
 Customer databases (read-only)                       Our side (self-hosted first)
 ┌────────────────────────────┐
 │ Postgres │ MySQL │ MSSQL │…│  ◄── adapter interface ──┐
 └────────────────────────────┘     introspect          │
   SQL runs INSIDE their DB:        profile (in-DB SQL) │
   aggregates + offending row IDs   constraints/indexes │
   come out, not bulk data          query logs          │
                                                         ▼
                                   ┌──────────────────────────────────────┐
                                   │ Metadata store (our Postgres)        │
                                   │  sources · assets/columns (identity +│
                                   │  versions) · profile time series ·   │
                                   │  relationships · rules · findings    │
                                   └──────────────────────────────────────┘
                                        │           │            │
                         ┌──────────────┘           │            └──────────────┐
                         ▼                          ▼                           ▼
              Discovery (relationships,   Detection (deterministic    Reasoning (AI: propose
              patterns)                   checks + statistics)        rules, explain, root cause)
                         └──────────────┬───────────┘                           │
                                        ▼                                       │
                                   Findings  ◄──────── human approve/reject ────┘
                                        │
                          Health score · Alerts · Docs/ER map · UI/API (Stage 4)
```

### Key design decisions
1. **Adapter per database, one interface.** Introspection uses SQLAlchemy's inspector, which works on almost every database; dialect-specific SQL is translated with sqlglot. Adding a database means writing an adapter, not changing the core.
2. **Compute in the customer's database.** Profiling and rule checks run as SQL there. Only aggregates and the IDs of offending rows come out.
3. **Safe on production.**
   - read-only transactions
   - `statement_timeout`
   - sampling (`TABLESAMPLE`) on large tables
   - cheap built-in statistics (`pg_stats`) where possible
   - off-peak scheduling and read replicas preferred
4. **Metadata store is our own Postgres.** Graph questions ("everything within 3 hops of `customers`") use recursive SQL queries; no graph database is needed at this scale.
5. **Three kinds of history:**
   - **Identity** rows never change (stable keys), so links and findings never break.
   - **Version** rows record structural change (type, nullability, constraints) with `valid_from`/`valid_to`.
   - **Profile** rows are the measurement time series (row counts, null rates, distinct counts) that detection reads.
6. **One `Finding` table** for every kind of output. Deterministic findings have no confidence score; AI-derived ones must carry confidence + evidence.
7. **Multi-tenant from day one** (`tenant_id` everywhere). Self-hosted installs use a single tenant; SaaS later uses many.

### Deployment
| Mode | When | Why |
|---|---|---|
| **Self-hosted** (Docker image inside the customer's network) | **v1** | Companies won't open production OLTP databases to an outside service. Data never leaves their network, which simplifies security reviews. |
| Hosted SaaS + lightweight agent | Later | Easier onboarding for warehouses and smaller teams |
| Marketplace / native apps (e.g. Snowflake Native App) | Later | Distribution |

### Privacy model
- Self-hosted means the metadata store lives in the **customer's** infrastructure.
- Raw values (e.g. top category values for "US / USA" detection) are stored only where needed, and can be disabled per source, table or column.
- **Nothing goes to an external AI model unless the customer opts in.** PII is detected and masked before any sample is sent.
- Raw values never appear in logs.

---

## 7. Roadmap

Each step has a **"done when"** and must pass before the next starts. Each layer is validated on real (or realistic) data before anything is built on top of it.

### Stage 0 — Setup ✅
Repo, tooling (ruff, mypy, pytest, pre-commit), CI config, local Postgres. *Remaining:* install Docker, push to GitHub so CI runs.

### Stage 1 — Core engine on PostgreSQL (CLI only)
| Step | What | Done when |
|---|---|---|
| 1.1 | **Metadata store schema** + migrations: tenants, data sources, ingestion runs, assets/columns (identity + versions), profile time series, relationships, rules, findings. Versioning helper. Graph queries. | Migrations up/down clean; constraint, versioning and graph-traversal tests pass |
| 1.2 | **Messy test lab + benchmark**: seeded generator for a realistic OLTP database (e-commerce/billing) with legacy naming, undeclared and misnamed FKs, composite keys and a nightly "ETL" simulator; an anomaly injector driven by a written answer key; a scorer that reports caught / missed / false alarms | One command builds the lab reproducibly; the scorer runs (scores near zero until detection exists) |
| 1.3 | **Postgres adapter**: introspection + safe in-DB profiling | Metadata store matches the lab DB by manual comparison |
| 1.4 | **Relationship discovery** + orphan/cardinality checks + **auto-documentation** (data dictionary + relationship map as Markdown/Mermaid) | Finds the lab's hidden FKs at the recall in the answer key; false positives explainable; the generated docs are readable and correct |
| 1.5 | **Detection**: structural, column-value, time-series (volume, freshness, nulls, distinct/duplicates), cold-start safe | Benchmark: every injected anomaly in categories 1–3 and 5 caught; false alarms explainable |
| 1.6 | Findings lifecycle (dedup, status), health score, alerts (Slack) + quiet-baseline mode | One command profiles, discovers, detects, scores and alerts sensibly |

### Stage 2 — AI-assisted discovery
LLM proposes business rules and ambiguous relationships from schema + profiles (+ samples if opted in). There's a review workflow, and approved rules are compiled to SQL checks. AI explanations of findings, row-level outliers (ML), and privacy-first masking before any AI call.
*Done when:* on the lab DB it proposes most hidden business rules with acceptable precision, and the approved rules catch the injected violations.

### Stage 3 — More databases + cross-system
Adapters for MySQL, SQL Server, Snowflake (trial already set up) and Oracle. Source-vs-warehouse reconciliation. DB-health add-on (Postgres first).

### Stage 4 — Product
FastAPI backend, auth, tenant isolation, web UI:
- findings with approve/reject
- rule review
- relationship map
- health dashboard

Also: onboarding scripts that create a read-only user per database type, a scheduler/workers, self-monitoring, an audit log, **self-hosted Docker packaging**, and **rules-as-code export** (YAML that runs in the customer's CI). A hosted SaaS control plane comes later.

### Stage 5 — Private beta
1–3 design partners with real messy databases. Security document. Track the confirm/reject rate of every finding and rule; this becomes the visible track record.

### Stage 6 — Root cause + fixes
**Change-aware detection** (line up anomalies with schema changes, deploys and ETL runs) → causal explanations with evidence → human-approved fix SQL.
*Go/no-go:* correctly explains 3 real past incidents with known causes.

### Stage 7 — Launch
Website, docs, pricing, billing, SOC 2 preparation.

### Later / ideas backlog
- Link tables to application code (ORM models, SQL in the repo) to say which *feature* breaks
- Natural-language questions ("why did orders drop yesterday?")
- Business-user "trust badge" per table/dashboard
- BigQuery, Databricks, MongoDB adapters
- Warehouse cost diagnosis (from the original brief)

---

## 8. The test lab and benchmark (why it matters)

We validate on a database built to be messy **on purpose**, with a written answer key:
- **Realistic schema:** customers, orders, order lines, products, invoices, payments, plus a legacy subsystem with `CUSTID`-style names, composite keys and few declared FKs.
- **Nightly ETL simulator:** each run adds or changes data so the history grows like a real system.
- **Anomaly injector:** each anomaly (a null spike, orphans, a dropped column, a failed load, a business-rule violation) is recorded in `validation/answer_keys/` **before** detection runs.
- **Scorer:** compares findings with the answer key and reports caught / missed / false alarms per category.

This gives us:
1. A regression test for the whole product. Every release is scored, so quality can't silently drop.
2. A real number for pitches: "caught 47 of 50 injected anomalies, 2 false alarms".
3. A safe place to tune thresholds before touching a real customer database.

---

## 9. Risks and how we handle them

| Risk | Mitigation |
|---|---|
| **False positives erode trust** | Learned baselines, cold-start handling, quiet first run, feedback loop, evidence on every finding, visible accuracy record |
| **Load on production databases** | Read-only transactions, timeouts, sampling, `pg_stats`, replicas, off-peak scheduling |
| **AI hallucinates rules or relationships** | AI only proposes; humans approve; checks are deterministic; confidence + evidence required |
| **Too many database dialects** | SQLAlchemy inspector + sqlglot; one adapter at a time, Postgres first |
| **Security reviews block adoption** | Self-hosted first; data stays in the customer's network; AI opt-in; audit log |
| **Crowded market** | Wedge = messy/legacy and OLTP databases + relationship discovery + AI-proposed rules, which warehouse-first tools don't do well |
| **Building without real data** | Test lab from Stage 1.2; design partners sought during Stage 1 |

---

## 10. Glossary
- **Adapter:** the code that talks to one kind of database (Postgres, MySQL, …) through a common interface.
- **Asset:** a table or view.
- **Profile:** a set of measurements of a table or column at one point in time (row count, null rate, distinct count, …).
- **Baseline:** what "normal" looks like for a measurement, learned from its history.
- **Finding:** one detected problem, with evidence and a status (open / confirmed / rejected / resolved).
- **Relationship:** a link between columns of two tables (a declared or inferred foreign key).
- **Rule:** an explicit expectation (e.g. `ship_date ≥ order_date`), proposed by AI or a human and approved before it runs.
- **Ingestion run:** one pass of reading metadata and profiles from a source.
- **Quiet baseline:** first-run mode that records findings but sends no alerts.
- **Cold start:** too little history to judge "normal" yet.
