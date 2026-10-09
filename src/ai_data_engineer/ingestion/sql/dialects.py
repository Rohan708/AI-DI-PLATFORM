"""What differs between databases, in one place. Everything else in the generic adapter
is built with SQLAlchemy Core, so quoting, letter case (Snowflake and Oracle fold to
upper case), LIMIT/TOP/FETCH and parameter styles come from SQLAlchemy's own dialects.

Per database we only need:
- how to make a session read-only and time-limited,
- where the database keeps row-count estimates and its query log,
- three function names that aren't standard (sample standard deviation, text length,
  natural log),
- which schemas are the database's own (never scanned).

Statement templates are filled with validated integers from ``ScanSettings`` only (SET
statements can't take bind parameters).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import TextClause, text

from ai_data_engineer.ingestion.settings import ScanSettings

SessionSql = Callable[[ScanSettings], list[str]]


def _none(_settings: ScanSettings) -> list[str]:
    return []


@dataclass(frozen=True)
class DialectProfile:
    name: str  # SQLAlchemy dialect name
    sqlglot: str  # sqlglot dialect for parsing the query log
    system_schemas: frozenset[str]
    # Scan only the connection's own schema/database unless include_schemas says more
    # (MySQL lists every database on the server; Oracle every user).
    default_schema_only: bool = False
    session_sql: SessionSql = _none  # run once per connection, then committed
    transaction_sql: SessionSql = _none  # run first inside each read-only transaction
    # (schema, table, rows, bytes) for every table; names compared case-insensitively.
    estimates_sql: TextClause | None = None
    # (query, calls), most called first; takes :limit. Failure = no query-log evidence.
    query_log_sql: TextClause | None = None
    stddev: str = "stddev_samp"
    length: str = "length"
    ln: str = "ln"
    # Read-only is enforced by the login's grants (see docs/setup/), not by the session.
    read_only_by_grants_only: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


def _postgres_txn(s: ScanSettings) -> list[str]:
    return [
        "SET TRANSACTION READ ONLY",
        f"SET LOCAL statement_timeout = {int(s.statement_timeout_ms)}",
        f"SET LOCAL lock_timeout = {int(s.lock_timeout_ms)}",
    ]


def _mysql_session(s: ScanSettings) -> list[str]:
    return [
        "SET SESSION TRANSACTION READ ONLY",
        f"SET SESSION max_execution_time = {int(s.statement_timeout_ms)}",  # SELECTs, in ms
        f"SET SESSION innodb_lock_wait_timeout = {max(1, int(s.lock_timeout_ms) // 1000)}",
    ]


def _mssql_session(s: ScanSettings) -> list[str]:
    # SQL Server has no read-only transactions and no per-session statement timeout:
    # read-only comes from the login (db_datareader only), and ApplicationIntent=ReadOnly
    # in the URL routes to a readable secondary when there is one.
    return [f"SET LOCK_TIMEOUT {int(s.lock_timeout_ms)}"]


def _oracle_txn(_s: ScanSettings) -> list[str]:
    return ["SET TRANSACTION READ ONLY"]  # the timeout is the driver's call_timeout


def _snowflake_session(s: ScanSettings) -> list[str]:
    seconds = max(1, int(s.statement_timeout_ms) // 1000)
    return [f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {seconds}"]


PROFILES: dict[str, DialectProfile] = {
    "postgresql": DialectProfile(
        name="postgresql",
        sqlglot="postgres",
        system_schemas=frozenset({"pg_catalog", "information_schema", "pg_toast"}),
        transaction_sql=_postgres_txn,
        estimates_sql=text(
            "SELECT n.nspname, c.relname, "
            "CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END, "
            "pg_total_relation_size(c.oid) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p', 'm')"
        ),
        query_log_sql=text(
            "SELECT query, calls FROM pg_stat_statements WHERE dbid = "
            "(SELECT oid FROM pg_database WHERE datname = current_database()) "
            "ORDER BY calls DESC LIMIT :limit"
        ),
    ),
    "mysql": DialectProfile(
        name="mysql",
        sqlglot="mysql",
        system_schemas=frozenset({"mysql", "information_schema", "performance_schema", "sys"}),
        default_schema_only=True,
        session_sql=_mysql_session,
        estimates_sql=text(
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_ROWS, DATA_LENGTH + INDEX_LENGTH "
            "FROM information_schema.TABLES WHERE TABLE_TYPE = 'BASE TABLE'"
        ),
        # Normalised statements (constants replaced by ?), like pg_stat_statements.
        query_log_sql=text(
            "SELECT DIGEST_TEXT, COUNT_STAR FROM performance_schema."
            "events_statements_summary_by_digest WHERE SCHEMA_NAME = DATABASE() "
            "AND DIGEST_TEXT IS NOT NULL ORDER BY COUNT_STAR DESC LIMIT :limit"
        ),
        length="char_length",  # LENGTH() counts bytes in MySQL
    ),
    "mssql": DialectProfile(
        name="mssql",
        sqlglot="tsql",
        system_schemas=frozenset(
            {"sys", "INFORMATION_SCHEMA", "guest", "db_owner", "db_accessadmin",
             "db_securityadmin", "db_ddladmin", "db_backupoperator", "db_datareader",
             "db_datawriter", "db_denydatareader", "db_denydatawriter"}
        ),
        session_sql=_mssql_session,
        estimates_sql=text(
            "SELECT s.name, t.name, SUM(p.rows), NULL FROM sys.tables t "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1) "
            "GROUP BY s.name, t.name"
        ),
        # Statement text from the plan cache (not normalised); only join pairs are kept.
        query_log_sql=text(
            "SELECT TOP (:limit) st.text, qs.execution_count FROM sys.dm_exec_query_stats qs "
            "CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st "
            "ORDER BY qs.execution_count DESC"
        ),
        stddev="stdev",
        length="len",
        ln="log",
        read_only_by_grants_only=True,
        notes=("no per-session statement timeout: set one on the login or the driver",),
    ),
    "oracle": DialectProfile(
        name="oracle",
        sqlglot="oracle",
        system_schemas=frozenset({"sys", "system", "outln", "xdb", "ctxsys", "mdsys"}),
        default_schema_only=True,
        transaction_sql=_oracle_txn,
        estimates_sql=text("SELECT owner, table_name, num_rows, NULL FROM all_tables"),
        query_log_sql=text(
            "SELECT sql_text, executions FROM v$sql ORDER BY executions DESC "
            "FETCH FIRST :limit ROWS ONLY"
        ),
    ),
    "snowflake": DialectProfile(
        name="snowflake",
        sqlglot="snowflake",
        system_schemas=frozenset({"information_schema"}),
        session_sql=_snowflake_session,
        estimates_sql=text(
            "SELECT table_schema, table_name, row_count, bytes FROM information_schema.tables "
            "WHERE table_type = 'BASE TABLE'"
        ),
        query_log_sql=text(
            "SELECT query_text, count(*) AS calls FROM "
            "table(information_schema.query_history(result_limit => 10000)) "
            "WHERE query_type = 'SELECT' GROUP BY query_text ORDER BY calls DESC LIMIT :limit"
        ),
        read_only_by_grants_only=True,
        notes=("read-only comes from the role (see docs/setup/snowflake_setup.md)",),
    ),
}


class UnsupportedDialectError(LookupError):
    pass


def profile_for(dialect_name: str) -> DialectProfile:
    try:
        return PROFILES[dialect_name]
    except KeyError:
        raise UnsupportedDialectError(
            f"no dialect profile for {dialect_name!r} (supported: {', '.join(PROFILES)})"
        ) from None


def call_timeout_hook(settings: ScanSettings) -> Callable[[Any, Any], None]:
    """Oracle: python-oracledb cancels any call running longer than this (ms)."""

    def on_connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.call_timeout = int(settings.statement_timeout_ms)

    return on_connect
