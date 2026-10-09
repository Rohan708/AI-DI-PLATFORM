# Setup: connecting MySQL, SQL Server, Oracle and Snowflake

Postgres has its own adapter; every other database goes through the **generic SQL adapter** (Stage 3). Each source needs three things:
1. **a read-only login**
2. **its driver** (an optional install)
3. **a connection URL in `.env`**, under a name you choose and pass as `--connection-ref`

The URL is never stored in the metadata store, only the variable's name.

```
python -m ai_data_engineer source add <name> --kind <mysql|sqlserver|oracle|snowflake> --connection-ref <ENV_VAR_NAME>
python -m ai_data_engineer run <name>
```

## MySQL / MariaDB
```bash
python -m pip install -e ".[mysql]"
```
```sql
CREATE USER 'aide'@'%' IDENTIFIED BY '<strong password>';
GRANT SELECT, SHOW VIEW ON shopdb.* TO 'aide'@'%';
-- optional: lets discovery read the application's joins from the statement digests
GRANT SELECT ON performance_schema.events_statements_summary_by_digest TO 'aide'@'%';
```
`.env`:
```
SHOPDB_URL=mysql+pymysql://aide:<password>@db-host:3306/shopdb
```
**What aide does on each connection:**
- `SET SESSION TRANSACTION READ ONLY`
- `max_execution_time` (statement timeout)
- `innodb_lock_wait_timeout`

**Scope:** only the database named in the URL is scanned, unless `include_schemas` lists more.

## SQL Server
```bash
python -m pip install -e ".[sqlserver]"
```
You also need Microsoft's **ODBC Driver 18 for SQL Server** on the machine.
```sql
CREATE LOGIN aide WITH PASSWORD = '<strong password>';
USE shopdb;
CREATE USER aide FOR LOGIN aide;
ALTER ROLE db_datareader ADD MEMBER aide;
GRANT VIEW DATABASE STATE TO aide;  -- optional: query statistics for discovery
```
`.env`:
```
SHOP_MSSQL_URL=mssql+pyodbc://aide:<password>@db-host:1433/shopdb?driver=ODBC+Driver+18+for+SQL+Server&ApplicationIntent=ReadOnly&Encrypt=yes
```
**Read-only and timeouts:**
- SQL Server has no read-only transactions, so read-only comes from the login (`db_datareader` only).
- `ApplicationIntent=ReadOnly` routes to a readable secondary when there is one.
- aide sets `LOCK_TIMEOUT` itself. There's no per-session statement timeout, so set one on the driver (e.g. `&timeout=30`) or a Resource Governor pool if you need it.

## Oracle
```bash
python -m pip install -e ".[oracle]"
```
This uses python-oracledb in thin mode, so no Oracle client is needed.
```sql
CREATE USER aide IDENTIFIED BY "<strong password>";
GRANT CREATE SESSION TO aide;
GRANT SELECT ANY TABLE TO aide;           -- or SELECT on each table of the app schema
GRANT SELECT ON v_$sql TO aide;           -- optional: query statistics for discovery
```
`.env`:
```
SHOP_ORACLE_URL=oracle+oracledb://aide:<password>@db-host:1521/?service_name=SHOPPDB
```
**Read-only and timeouts:** each transaction starts with `SET TRANSACTION READ ONLY`, and the driver's `call_timeout` cancels long calls.

**Scope:** only the login's default schema is scanned. Use `--include-schemas SHOP` to scan the application's schema instead.

## Snowflake
```bash
python -m pip install -e ".[snowflake]"
```
Use the read-only role and key-pair user from [`snowflake_setup.md`](snowflake_setup.md) (`AIDE_SVC`, role `AIDE_READONLY`).

`.env`:
```
JAFFLE_SF_URL=snowflake://AIDE_SVC@<account>/JAFFLE_SHOP/PUBLIC?warehouse=AIDE_WH&role=AIDE_READONLY&private_key_file=C:/keys/aide_svc_key.p8
```
**Read-only and timeouts:** read-only comes from the role. aide sets `STATEMENT_TIMEOUT_IN_SECONDS` per session. Queries run in your warehouse, so they cost credits; use an X-Small warehouse with auto-suspend.

## What works where
| | Postgres | MySQL | SQL Server | Oracle | Snowflake |
|---|---|---|---|---|---|
| Scan (structure + profiles) | ✅ tuned | ✅ | ✅ | ✅ | ✅ |
| Relationship discovery (names + values) | ✅ | ✅ | ✅ | ✅ | ✅ |
| Query-log evidence | `pg_stat_statements` | statement digests | plan cache | `v$sql` | query history |
| Rules, row outliers, detection | ✅ | ✅ | ✅ | ✅ | ✅ |
| Reconciliation (as either side) | ✅ | ✅ | ✅ | ✅ | ✅ |
| Database health | ✅ | later | later | later | later |
| Sampling of huge tables | random (`TABLESAMPLE`) | first N rows | first N rows | first N rows | first N rows |
| Tested by | integration tests | integration tests (Docker) | SQL compile tests | SQL compile tests | your trial account (manual) |

**On sampling:** above 1M rows, the generic adapter reads the first 1M rows the database returns. That's cheap and works everywhere, but it isn't random, so findings on sampled tables say so.
