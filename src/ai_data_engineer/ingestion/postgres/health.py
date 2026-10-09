"""Database-health readings for Postgres (Stage 3 add-on), from its own statistics views.
Read-only, and nothing from the data itself: object names, counts, sizes and durations.
Slow queries are reported with pg_stat_statements' normalised text (constants replaced
by placeholders); sessions stuck in a transaction are reported without their query text,
which can contain values.

Each reading runs in its own transaction (a missing extension or permission must not
abort the others) and reports whether it could run, so findings it can't re-check are
not resolved by mistake.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from ai_data_engineer.ingestion.base import HealthIssue, HealthLimits, HealthReading

UNUSED_INDEX = "unused_index"
TABLE_BLOAT = "table_bloat"
SLOW_QUERY = "slow_query"
IDLE_IN_TRANSACTION = "idle_in_transaction"
SEQUENCE_EXHAUSTION = "sequence_exhaustion"


_STATS_AGE_DAYS = text(
    "SELECT extract(epoch FROM now() - coalesce(stats_reset, pg_postmaster_start_time())) "
    "/ 86400 FROM pg_stat_database WHERE datname = current_database()"
)
_UNUSED_INDEXES = text(
    "SELECT s.schemaname, s.relname, s.indexrelname, pg_relation_size(s.indexrelid) AS bytes "
    "FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid = s.indexrelid "
    "WHERE NOT i.indisunique AND NOT i.indisprimary AND s.idx_scan = 0 "
    "AND pg_relation_size(s.indexrelid) >= :min_bytes ORDER BY bytes DESC"
)
_BLOAT = text(
    "SELECT schemaname, relname, n_live_tup, n_dead_tup, "
    "greatest(last_vacuum, last_autovacuum) AS last_vacuum FROM pg_stat_user_tables "
    "WHERE n_dead_tup >= :min_dead AND n_dead_tup >= :ratio * (n_live_tup + n_dead_tup)"
)
_SLOW = text(
    "SELECT queryid, query, calls, mean_exec_time, total_exec_time FROM pg_stat_statements "
    "WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND userid <> (SELECT oid FROM pg_roles WHERE rolname = current_user) "
    "AND calls >= :min_calls AND mean_exec_time >= :min_ms "
    "ORDER BY total_exec_time DESC LIMIT 20"
)
_IDLE = text(
    "SELECT count(*) AS sessions, max(extract(epoch FROM now() - state_change)) AS longest, "
    "string_agg(DISTINCT coalesce(nullif(application_name, ''), '?'), ', ') AS apps "
    "FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() "
    "AND state LIKE 'idle in transaction%' "
    "AND now() - state_change > make_interval(secs => :min_seconds)"
)
_SEQUENCES = text(
    "SELECT schemaname, sequencename, last_value, max_value FROM pg_sequences "
    "WHERE last_value IS NOT NULL AND max_value > 0 "
    "AND last_value::numeric / max_value >= :max_used"
)


def readings(limits: HealthLimits) -> dict[str, Callable[[Connection], list[HealthIssue]]]:
    """One function per health check; the adapter runs each in its own transaction."""
    return {
        UNUSED_INDEX: lambda c: _unused_indexes(c, limits),
        TABLE_BLOAT: lambda c: _bloat(c, limits),
        SLOW_QUERY: lambda c: _slow(c, limits),
        IDLE_IN_TRANSACTION: lambda c: _idle(c, limits),
        SEQUENCE_EXHAUSTION: lambda c: _sequences(c, limits),
    }


def _unused_indexes(conn: Connection, limits: HealthLimits) -> list[HealthIssue]:
    age = float(conn.execute(_STATS_AGE_DAYS).scalar() or 0)
    if age < limits.unused_index_min_stats_days:
        raise HealthReadingSkipped(f"usage statistics cover only {age:.1f} days")
    return [
        HealthIssue(
            UNUSED_INDEX,
            subject=f"{r.schemaname}.{r.indexrelname}",
            table_ref=f"{r.schemaname}.{r.relname}",
            numbers={"bytes": int(r.bytes), "scans": 0, "stats_days": round(age, 1)},
            summary=(
                f"index {r.schemaname}.{r.indexrelname} on {r.relname} "
                f"({_size(int(r.bytes))}) was never used in {age:.0f} days"
            ),
        )
        for r in conn.execute(_UNUSED_INDEXES, {"min_bytes": limits.unused_index_min_bytes})
    ]


def _bloat(conn: Connection, limits: HealthLimits) -> list[HealthIssue]:
    rows = conn.execute(
        _BLOAT, {"min_dead": limits.bloat_min_dead_rows, "ratio": limits.bloat_min_dead_ratio}
    )
    out = []
    for r in rows:
        total = int(r.n_live_tup) + int(r.n_dead_tup)
        share = int(r.n_dead_tup) / total if total else 0.0
        vacuumed = r.last_vacuum.isoformat() if r.last_vacuum else "never"
        out.append(
            HealthIssue(
                TABLE_BLOAT,
                subject=f"{r.schemaname}.{r.relname}",
                table_ref=f"{r.schemaname}.{r.relname}",
                numbers={"dead_rows": int(r.n_dead_tup), "live_rows": int(r.n_live_tup),
                         "dead_share": round(share, 4), "last_vacuum": vacuumed},
                summary=(
                    f"{r.schemaname}.{r.relname} has {int(r.n_dead_tup):,} dead rows "
                    f"({share:.0%} of the table); last vacuum: {vacuumed}"
                ),
            )
        )
    return out


def _slow(conn: Connection, limits: HealthLimits) -> list[HealthIssue]:
    rows = conn.execute(
        _SLOW, {"min_calls": limits.slow_query_min_calls, "min_ms": limits.slow_query_min_mean_ms}
    )
    return [
        HealthIssue(
            SLOW_QUERY,
            subject=f"query {r.queryid}",
            table_ref=None,
            numbers={"calls": int(r.calls), "mean_ms": round(float(r.mean_exec_time), 1),
                     "total_ms": round(float(r.total_exec_time), 1),
                     "statement": " ".join(str(r.query).split())[:300]},
            summary=(
                f"a query called {int(r.calls):,} times takes {float(r.mean_exec_time):,.0f} ms "
                f"on average: {' '.join(str(r.query).split())[:80]}"
            ),
        )
        for r in rows
    ]


def _idle(conn: Connection, limits: HealthLimits) -> list[HealthIssue]:
    r = conn.execute(_IDLE, {"min_seconds": limits.idle_transaction_min_seconds}).one()
    if not r.sessions:
        return []
    minutes = float(r.longest or 0) / 60
    return [
        HealthIssue(
            IDLE_IN_TRANSACTION,
            subject="idle in transaction",
            table_ref=None,
            numbers={"sessions": int(r.sessions), "longest_minutes": round(minutes, 1),
                     "applications": r.apps},
            summary=(
                f"{int(r.sessions)} sessions sit idle inside an open transaction (longest "
                f"{minutes:.0f} min; apps: {r.apps}); they hold locks and block vacuum"
            ),
        )
    ]


def _sequences(conn: Connection, limits: HealthLimits) -> list[HealthIssue]:
    out = []
    for r in conn.execute(_SEQUENCES, {"max_used": limits.sequence_max_used}):
        used = int(r.last_value) / int(r.max_value)
        out.append(
            HealthIssue(
                SEQUENCE_EXHAUSTION,
                subject=f"{r.schemaname}.{r.sequencename}",
                table_ref=None,
                numbers={"last_value": int(r.last_value), "max_value": int(r.max_value),
                         "used": round(used, 4)},
                summary=(
                    f"sequence {r.schemaname}.{r.sequencename} has used {used:.0%} of its "
                    f"range ({int(r.last_value):,} of {int(r.max_value):,}); inserts fail "
                    "when it runs out"
                ),
            )
        )
    return out


class HealthReadingSkipped(Exception):  # noqa: N818 - a signal, not an error
    """The reading can't be judged right now (e.g. statistics too young)."""


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n //= 1024
    return f"{n:.0f} TB"


def run_readings(
    connect: Callable[[], AbstractContextManager[Connection]], limits: HealthLimits
) -> HealthReading:
    """Run every reading in its own read-only transaction (``connect`` yields one)."""
    issues: list[HealthIssue] = []
    ran: list[str] = []
    skipped: dict[str, str] = {}
    for name, read in readings(limits).items():
        try:
            with connect() as conn:
                issues += read(conn)
            ran.append(name)
        except HealthReadingSkipped as exc:
            skipped[name] = str(exc)
        except DBAPIError as exc:  # missing extension / permission: that reading is skipped
            skipped[name] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
    return HealthReading(issues=issues, checks_run=tuple(ran), skipped=skipped)
