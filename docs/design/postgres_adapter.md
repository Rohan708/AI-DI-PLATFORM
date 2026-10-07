# Postgres adapter & scanning (Stage 1.3)

*Code: `src/ai_data_engineer/ingestion/`. Commands: `aide source …`, `aide scan …`. Safety rationale: [ADR 0005](../decisions/0005-scanning-safety.md).*

This is how the tool reads a customer's database. Postgres is first; every other database implements the same interface later (Stage 3).

```
aide scan shopco
   │
   ├─ open a run (ingestion_run)
   ├─ adapter.introspect()   ── catalog only, no table data ──►  structure
   │     └─ graph.versioning.record_asset()  → new versions only on real changes
   │     └─ mark_missing_assets()            → dropped tables marked deleted
   ├─ adapter.profile(table) ── one summary query per table ──►  measurements
   │     └─ asset_profile + column_profile rows
   └─ close the run: succeeded | partial (some tables failed) | failed
```

---

## 1. The adapter interface (`ingestion/base.py`)
Every database adapter provides two things:

| Method | Returns | Reads table data? |
|---|---|---|
| `introspect()` | `DiscoveredTable` per table or view: an `AssetObservation` (columns, types, keys, indexes, declared FKs, comments), plus the database's own row estimate and size | **No**, catalog only |
| `profile(table)` | `TableMeasurement`: row count, size, sample fraction, per-column `ColumnMeasurement` | Yes, but **aggregates only**, computed inside the database |

Adapters never write to the metadata store; `ingestion/scan.py` does. Only tables and materialized views are profiled. Views are described but not profiled, because querying a view can be arbitrarily expensive.

## 2. Safety: how we never harm a customer database
Every query runs inside `PostgresAdapter.read_only()`:

| Protection | How |
|---|---|
| Can't write | `SET TRANSACTION READ ONLY`: Postgres itself rejects any write (a test proves it) |
| Can't run long | `SET LOCAL statement_timeout` (default **30 s**) |
| Can't block the app | `SET LOCAL lock_timeout` (default **2 s**) |
| Visible to their DBA | `application_name = 'aide'` shows up in `pg_stat_activity` |
| One slow table can't sink a scan | each table is profiled in its own transaction; a failure marks the run `partial` and the rest continue |
| Big tables stay cheap | sampled above a threshold (below) |

For customers we ship [`scripts/postgres/create_readonly_user.sql`](../../scripts/postgres/create_readonly_user.sql). It creates a login that can only `SELECT`, defaults every session to read-only, and has `pg_read_all_stats` for query statistics.

## 3. Reading structure (`postgres/catalog.py`)
Four catalog queries, which touch no customer data:

| Query | Gives |
|---|---|
| `pg_class` + `pg_namespace` | tables (`r`, partitioned `p`), views (`v`), materialized views (`m`), foreign tables (`f`); row estimate (`reltuples`), size, comment. Partitions are skipped; the parent represents them. |
| `pg_attribute` + `pg_type` | columns: native type (`format_type`, e.g. `character varying(255)`), type family, nullability, default, comment |
| `pg_constraint` | primary key, unique constraints, declared FKs (including **composite** ones, in column order) |
| `pg_index` | indexes: name, columns/expressions, unique, primary |

System schemas and **objects owned by extensions** (e.g. the `pg_stat_statements` views an extension creates in `public`) are always skipped: they aren't customer data. Per-source `include_schemas` / `exclude_schemas` filter the rest; the lab excludes its bookkeeping schema `aide_lab`.

**Type families** (`postgres/types.py`) map native types to neutral families: `int2/4/8 → integer`, `numeric → decimal`, `float4/8 → float`, `varchar/text/char/enums → string`, `timestamp(tz) → timestamp`, `bool → boolean`, `json(b) → json`, `uuid → uuid`, arrays → `array`, anything else → `other`.

## 4. Measuring (`postgres/profiling.py`)
**One query per table** measures every column at once:
```sql
SELECT count(*) AS row_count,
       count("cust_no") AS c0_nonnull, count(DISTINCT "cust_no") AS c0_distinct,
       min("cust_no")::text AS c0_min, max("cust_no")::text AS c0_max,
       avg("cust_no")::float8 AS c0_mean, stddev_samp("cust_no")::float8 AS c0_stddev,
       ...
FROM "shop"."orders"
```

What gets measured depends on the type family:

| Family | null count | distinct | min/max | mean/stddev | avg length | top values |
|---|---|---|---|---|---|---|
| integer / decimal / float | ✓ | ✓ | ✓ | ✓ | | |
| date / timestamp / time / interval | ✓ | ✓ | ✓ | | | |
| string | ✓ | ✓ | ✓ if values allowed | | ✓ | ✓ if ≤ 50 distinct and values allowed |
| boolean | ✓ | ✓ | | | | ✓ |
| uuid | ✓ | ✓ | | | | |
| json / array / binary / other | ✓ | | | | | |

- **Top values** are fetched with one extra small `GROUP BY` query per qualifying column. They're what lets detection see `US` / `USA` / `United States`.
- **Sampling:** if the database estimates more rows than `sample_row_threshold` (default **1,000,000**), the table is read with `TABLESAMPLE SYSTEM (p) REPEATABLE (0)`, where p is chosen to read about the threshold. The run records `sample_fraction`, sets `row_count_is_estimate`, and flags distinct counts as approximate. A fixed seed means repeated scans of an unchanged table read the same blocks.
- **Freshness:** Postgres keeps no "last modified" time. We store (a) the max of every timestamp column (`max_repr`, e.g. `reporting.daily_sales.loaded_at`) and (b) the table's activity counters from `pg_stat_user_tables` (`n_tup_ins`, `n_tup_upd`, `n_tup_del`, `n_live_tup`, last analyse) in `asset_profile.properties.pg_stat`. Stage 1.5's freshness check uses these. Nothing is written to the customer database and no triggers are added.
- **Privacy:** `allow_value_samples = false` turns off everything that stores actual values (top values, min/max of text). Numbers and dates are aggregates, not identifying values.

## 5. Scan settings (`data_source.settings`)
Validated on `aide source add`. Unknown keys are rejected so typos fail loudly. Only non-default values are stored.

| Setting | Default | Meaning |
|---|---|---|
| `include_schemas` | all | only scan these |
| `exclude_schemas` | none | never scan these (wins over include) |
| `statement_timeout_ms` | 30000 | per-query time limit |
| `lock_timeout_ms` | 2000 | max wait for a lock |
| `sample_row_threshold` | 1000000 | sample tables estimated above this |
| `top_values_max_distinct` | 50 | collect top values only up to this many distinct values |
| `allow_value_samples` | true | store actual values (top values, text min/max) |

## 6. Credentials (`connections.py`)
`data_source.connection_ref` is the **name** of an environment variable, looked up in the process environment first and then in `.env`. Its value is the connection URL. The metadata store never holds credentials. A secrets-manager backend can replace this one function later.

## 7. The scan (`scan.py`)
- `scan_source(session, source, observed_at=None)` returns a `ScanResult` (status, tables seen/profiled/created/changed/removed, errors). The caller commits.
- Run statuses:
  - `succeeded`
  - `partial`: some tables failed to profile; their errors are in `error_message`, everything else is kept
  - `failed`: couldn't connect or read the structure
- `run.stats` keeps counts, changed tables and the duration.
- `observed_at` lets the lab stamp scans with **simulated** dates.

## 8. Commands
| Command | Does |
|---|---|
| `aide source add NAME --connection-ref ENV_VAR [--exclude-schemas a,b] [--include-schemas …] [--sample-row-threshold N] [--no-value-samples]` | register a database |
| `aide source list` | registered databases |
| `aide source show NAME` | what we know: every table (kind, rows, PK) and column (type, family, nullable, null %, distinct) from the latest scan |
| `aide scan NAME [--as-of ISO-DATETIME]` | scan now; exit code 1 unless it fully succeeded |
| `aide lab run … --scan NAME` / `aide lab tick … --scan NAME` | scan the lab after every simulated day (at 03:00 simulated time) |

## 9. Known limitations (tracked in CLAUDE.md backlog)
- Views are not profiled.
- Renames look like drop + add (no rename detection yet).
- History must be recorded in time order. Re-running a lab plan against the **same** data source restarts the simulated calendar earlier than existing scans, so use a fresh source name per lab run until `aide source remove` exists.
- Query-log reading (`pg_stat_statements`) comes in Stage 1.4.
