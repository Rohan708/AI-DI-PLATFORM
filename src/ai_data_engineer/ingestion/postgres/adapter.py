"""The Postgres adapter: read-only structure + in-database profiling."""

from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Self

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from ai_data_engineer.ingestion.base import (
    DiscoveredTable,
    InclusionResult,
    KeyRef,
    OrphanResult,
    QueryStat,
    TableMeasurement,
)
from ai_data_engineer.ingestion.postgres import relationships
from ai_data_engineer.ingestion.postgres.catalog import read_activity, read_structure
from ai_data_engineer.ingestion.postgres.profiling import run_profile
from ai_data_engineer.ingestion.settings import ScanSettings

# Shown in pg_stat_activity, so the customer's DBA can see (and identify) our queries.
APPLICATION_NAME = "aide"


class PostgresAdapter:
    def __init__(self, url: str, settings: ScanSettings) -> None:
        self.settings = settings
        self._engine = create_engine(
            url, poolclass=NullPool, connect_args={"application_name": APPLICATION_NAME}
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._engine.dispose()

    @contextmanager
    def read_only(self) -> Iterator[Connection]:
        """A transaction the database itself refuses to write in, with time limits."""
        with self._engine.connect() as conn, conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            # SET can't take bind parameters; the values are validated ints from settings.
            conn.execute(
                text(f"SET LOCAL statement_timeout = {int(self.settings.statement_timeout_ms)}")
            )
            conn.execute(text(f"SET LOCAL lock_timeout = {int(self.settings.lock_timeout_ms)}"))
            yield conn

    def introspect(self) -> list[DiscoveredTable]:
        with self.read_only() as conn:
            return read_structure(conn, self.settings)

    def profile(self, table: DiscoveredTable) -> TableMeasurement:
        # One transaction per table: a timeout on one table doesn't affect the others.
        with self.read_only() as conn:
            plan, measured_rows, columns = run_profile(conn, table, self.settings)
            activity = read_activity(conn, int(table.native_id)) if table.native_id else {}
        if plan.sampled:
            row_count, is_estimate = table.estimated_rows, True
        else:
            row_count, is_estimate = measured_rows, False
        return TableMeasurement(
            row_count=row_count,
            row_count_is_estimate=is_estimate,
            size_bytes=table.size_bytes,
            sample_fraction=plan.sample_fraction,
            columns=tuple(columns),
            # Postgres keeps no "last modified" time; freshness is derived from the
            # activity counters and the max of timestamp columns instead.
            last_modified_at=None,
            properties={"pg_stat": activity, "rows_measured": measured_rows},
        )

    # --- relationship discovery (Stage 1.4) ------------------------------------------------

    def read_query_log(self, limit: int) -> list[QueryStat]:
        """Most-called statements from pg_stat_statements; empty if the extension isn't
        installed or readable (discovery then works without query-log evidence)."""
        try:
            with self.read_only() as conn:
                return relationships.read_query_log(conn, limit)
        except DBAPIError:
            return []

    def value_inclusion(self, child: KeyRef, parent: KeyRef, max_rows: int) -> InclusionResult:
        with self.read_only() as conn:
            return relationships.value_inclusion(
                conn, child, parent, max_rows, self.settings.sample_row_threshold
            )

    def count_orphans(
        self, child: KeyRef, parent: KeyRef, row_ids: tuple[str, ...], sample_size: int
    ) -> OrphanResult:
        with self.read_only() as conn:
            return relationships.count_orphans(
                conn, child, parent, row_ids, sample_size, self.settings.sample_row_threshold
            )
