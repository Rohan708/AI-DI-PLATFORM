# Design: more databases, reconciliation, database health (Stage 3)

**Status:** code written 2026-10-10; ruff + mypy clean 2026-10-11; tests not yet run.
**Setup per database:** [`docs/setup/databases.md`](../setup/databases.md).

## 1. One generic adapter (3.1)
**Decision:** we have one adapter for every SQL database instead of one per database. It's built on SQLAlchemy's inspector (structure) and SQLAlchemy Core (queries), so quoting, upper/lower-case folding (Snowflake and Oracle), `LIMIT` / `TOP` / `FETCH FIRST`, and parameter styles all come from SQLAlchemy's own dialects. Postgres keeps its tuned adapter (random sampling with `TABLESAMPLE`, `pg_class` estimates, activity counters).

What differs per database lives in one table, `ingestion/sql/dialects.py`:

| | read-only / limits | estimates | query log | functions |
|---|---|---|---|---|
| MySQL | `SET SESSION TRANSACTION READ ONLY`, `max_execution_time`, `innodb_lock_wait_timeout` | `information_schema.TABLES` | statement digests | `CHAR_LENGTH` |
| SQL Server | the login's grants; `SET LOCK_TIMEOUT` | `sys.partitions` | plan cache | `STDEV`, `LEN`, `LOG` |
| Oracle | `SET TRANSACTION READ ONLY`; driver `call_timeout` | `all_tables.num_rows` | `v$sql` | |
| Snowflake | the role's grants; `STATEMENT_TIMEOUT_IN_SECONDS` | `information_schema.tables` | `query_history()` | |

**Same contract as Postgres:**
- everything is counted inside the database
- only aggregates and row identifiers come back
- each operation runs in its own transaction, rolled back at the end

**Differences:**
- **Sampling** above 1M rows reads the *first* 1M rows (`LIMIT`), because that's portable. It isn't random, and findings say "sampled".
- **Medians** for row outliers come from `ORDER BY … OFFSET`, because `percentile_cont` isn't everywhere.
- **Types** map through SQLAlchemy's type classes. Exact numerics with scale 0 count as integers. Large text types (Oracle CLOB, SQL Server NTEXT) count as "other", because they can't be compared.

**Discovery across databases:** query-log entries carry their SQL dialect, so sqlglot parses MySQL's backticks or T-SQL brackets. Names from query logs match the catalog regardless of letter case.

**Tests:**
- On the **Postgres lab**, the generic adapter must give the same row, NULL and distinct counts and the same relationships as the tuned adapter.
- On **MySQL** (testcontainers), the whole engine runs end to end. A write inside the read-only session must fail.
- **SQL Server and Oracle:** every query is compiled with their dialects.
- **Snowflake:** a manual run on the trial account.

## 2. Reconciliation (3.2)
**The question:** "the warehouse copy should match the app database. Does it?"
- A **pair** (`aide reconcile add NAME --left app --right warehouse`) maps tables by name. Use `--schema-map shop=analytics` when the copy renames a schema, or `--table shop.orders=dw.fct_orders` for explicit tables. With an explicit list, a missing copy is itself a finding.
- **No extra queries.** Both sides were measured by their own nightly scans, so reconciliation compares the stored measurements. It works across any two adapters (Postgres app vs Snowflake warehouse).

**The rules** (`reconcile/compare.py`, all tolerances in `ReconcileSettings`):

| Compared | Difference when | Severity |
|---|---|---|
| row count | relative gap > 0.1% (5% if either count is an estimate) and at least 1 row | high |
| NULL rate (per column) | > 0.1 points apart | medium |
| distinct count | > 0.1% apart (skipped if either is estimated) | medium |
| numeric total (mean × non-NULL rows) | relative gap > 1e-6 | medium |
| min / max of numbers and dates | different values (compared as values, so `+00` and `+00:00` agree) | medium |

**Notes on these rules:**
- **One finding, not a cascade:** when the row counts differ, the columns aren't compared, because every total would differ too.
- **Timing:** scans more than 26 hours apart aren't compared, since the copy may simply be behind.
- **Findings:** category `cross_system`, check `reconcile_<metric>`. They belong to the left source, name both tables, and resolve when the difference is gone.
- **Schedule:** `aide run` compares every pair the source belongs to (the `reconcile` step).

## 3. Database health (3.3, Postgres first)
These problems are with the database rather than the data. Readings come from Postgres' statistics views: names, counts, sizes and durations only.

| Check | Finding when (defaults in `DbHealthSettings`) | Severity |
|---|---|---|
| `db_unused_index` | a non-unique index ≥ 10 MB was never scanned, and the usage statistics cover ≥ 7 days | low |
| `db_table_bloat` | ≥ 10,000 dead rows and ≥ 20% of the table | medium |
| `db_slow_query` | a query called ≥ 10 times averages ≥ 1 s (pg_stat_statements' normalised text, constants replaced) | medium |
| `db_idle_in_transaction` | sessions idle inside an open transaction > 5 min (no query text: it can contain values) | medium |
| `db_sequence_exhaustion` | a sequence used ≥ 80% of its range (inserts fail when it runs out) | high |

**How readings behave:**
- Each reading runs in its own transaction, and reports whether it ran. Only readings that ran can resolve findings, so a missing extension or permission never closes a finding by mistake.
- It runs as the `dbhealth` step of `aide run`, or with `aide dbhealth NAME`.
- Other databases return "not yet".

## 4. Nightly order
```
scan → discover → rules → rows → dbhealth → detect → reconcile → health → alert
```

## 5. Known limitations
- Sampling in the generic adapter isn't random (see §1).
- SQL Server has no per-session statement timeout; set one on the driver.
- Database health is Postgres-only. MySQL, SQL Server and Oracle equivalents (`sys.schema_unused_indexes`, `sys.dm_db_index_usage_stats`, …) come later.
- Reconciliation compares aggregates, so a copy with the same counts and totals but *different rows* isn't caught. Key-level comparison (hash buckets of primary keys) is planned.
- Oracle and SQL Server are only checked by compiling their SQL here. The first real run on each may need small fixes.

## 6. Results log
| Date | Run | Result | Notes |
|---|---|---|---|
| | | | |
