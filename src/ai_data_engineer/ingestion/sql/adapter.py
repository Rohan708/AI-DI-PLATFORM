"""One adapter for every SQL database SQLAlchemy can reach: MySQL / MariaDB, SQL Server,
Oracle, Snowflake (and Postgres, which normally uses its own tuned adapter).

Same contract as ``PostgresAdapter``: read-only, time-limited transactions; structure
from the catalog; all measuring inside the database. What differs per database is in
``dialects.py``; the queries are SQLAlchemy Core (``queries.py``).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Self

from sqlalchemy import Connection, create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from ai_data_engineer.ingestion.base import (
    DiscoveredTable,
    HealthLimits,
    HealthReading,
    InclusionResult,
    KeyRef,
    OrphanResult,
    OutlierResult,
    QueryStat,
    RuleResult,
    TableMeasurement,
)
from ai_data_engineer.ingestion.settings import ScanSettings
from ai_data_engineer.ingestion.sql import catalog, queries
from ai_data_engineer.ingestion.sql.dialects import call_timeout_hook, profile_for
from ai_data_engineer.rules.spec import RuleSpec


class SqlAdapter:
    def __init__(self, url: str, settings: ScanSettings) -> None:
        self.settings = settings
        self._engine = create_engine(url, poolclass=NullPool)
        self.dialect = profile_for(self._engine.dialect.name)
        if self.dialect.name == "oracle":
            event.listen(self._engine, "connect", call_timeout_hook(settings))

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
        """Session limits first (committed), then a transaction that starts read-only."""
        with self._engine.connect() as conn:
            for statement in self.dialect.session_sql(self.settings):
                conn.execute(text(statement))
            conn.commit()
            transaction = conn.begin()
            try:
                for statement in self.dialect.transaction_sql(self.settings):
                    conn.execute(text(statement))
                yield conn
            finally:
                # Nothing we run writes; ending with a rollback makes sure of it.
                transaction.rollback()

    # --- structure and profiling -----------------------------------------------------------

    def introspect(self) -> list[DiscoveredTable]:
        with self.read_only() as conn:
            return catalog.read_structure(conn, self.dialect, self.settings)

    def profile(self, table: DiscoveredTable) -> TableMeasurement:
        with self.read_only() as conn:
            fraction, measured, columns = queries.run_profile(
                conn, table, self.settings, self.dialect
            )
        sampled = fraction < 1.0
        return TableMeasurement(
            row_count=table.estimated_rows if sampled else measured,
            row_count_is_estimate=sampled,
            size_bytes=table.size_bytes,
            sample_fraction=fraction,
            columns=tuple(columns),
            last_modified_at=None,
            properties={"rows_measured": measured, "dialect": self.dialect.name},
        )

    # --- relationship discovery ----------------------------------------------------------

    def read_query_log(self, limit: int) -> list[QueryStat]:
        if self.dialect.query_log_sql is None:
            return []
        try:
            with self.read_only() as conn:
                rows = conn.execute(self.dialect.query_log_sql, {"limit": limit}).all()
        except DBAPIError:
            return []  # not enabled or no permission: discovery works without it
        return [QueryStat(str(r[0]), int(r[1]), self.dialect.sqlglot) for r in rows if r[0]]

    def value_inclusion(self, child: KeyRef, parent: KeyRef, max_rows: int) -> InclusionResult:
        with self.read_only() as conn:
            return queries.value_inclusion(
                conn, child, parent, max_rows, self.settings.sample_row_threshold
            )

    def count_orphans(
        self, child: KeyRef, parent: KeyRef, row_ids: tuple[str, ...], sample_size: int
    ) -> OrphanResult:
        with self.read_only() as conn:
            return queries.count_orphans(
                conn, child, parent, row_ids, sample_size, self.settings.sample_row_threshold
            )

    # --- rules and row outliers (Stage 2) --------------------------------------------------

    def check_rule(
        self,
        spec: RuleSpec,
        row_ids: tuple[str, ...],
        estimated_rows: int | None,
        sample_size: int,
    ) -> RuleResult:
        with self.read_only() as conn:
            return queries.check_rule(
                conn, spec, row_ids, estimated_rows, sample_size,
                self.settings.sample_row_threshold,
            )  # fmt: skip

    def row_outliers(
        self,
        column: KeyRef,
        row_ids: tuple[str, ...],
        *,
        sigmas: float,
        min_ratio: float,
        min_spread: float,
        sample_size: int,
    ) -> OutlierResult:
        with self.read_only() as conn:
            return queries.row_outliers(
                conn, column, row_ids, self.dialect, sigmas=sigmas, min_ratio=min_ratio,
                min_spread=min_spread, sample_size=sample_size,
                threshold=self.settings.sample_row_threshold,
            )  # fmt: skip

    # --- database health ----------------------------------------------------------------------

    def db_health(self, limits: HealthLimits) -> HealthReading:
        """Not yet for these databases (Postgres first): an empty reading, nothing resolved."""
        return HealthReading(issues=[], checks_run=(), skipped={"all": "not implemented"})
