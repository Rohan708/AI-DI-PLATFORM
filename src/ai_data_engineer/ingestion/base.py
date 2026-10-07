"""The adapter interface every database type implements.

An adapter only *reads*: it describes the structure (``introspect``) and measures tables
(``profile``), computing everything inside the customer database. It never writes to the
metadata store; ``ingestion.scan`` does that. Postgres is first; MySQL, SQL Server and
Snowflake implement the same contract later.
"""

from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Protocol, Self

from ai_data_engineer.graph.models import AssetKind
from ai_data_engineer.graph.versioning import AssetObservation

# Only these kinds hold their own data; views are described but not profiled (querying a
# view can be arbitrarily expensive).
PROFILABLE_KINDS = (AssetKind.TABLE, AssetKind.MATERIALIZED_VIEW)


@dataclass(frozen=True)
class DiscoveredTable:
    observation: AssetObservation
    estimated_rows: int | None  # the database's own estimate, used to decide on sampling
    size_bytes: int | None
    native_id: Any = None  # adapter-specific handle (e.g. a Postgres oid)

    @property
    def ref(self) -> str:
        return f"{self.observation.schema_name}.{self.observation.name}"

    @property
    def profilable(self) -> bool:
        return self.observation.kind in PROFILABLE_KINDS


@dataclass(frozen=True)
class ColumnMeasurement:
    name: str
    row_count: int  # rows measured (the sample size when sampled)
    null_count: int
    distinct_count: int | None
    distinct_is_approx: bool
    min_repr: str | None
    max_repr: str | None
    mean: float | None = None
    stddev: float | None = None
    avg_length: float | None = None
    top_values: list[dict[str, Any]] | None = None
    distinct_case_insensitive: int | None = None  # text columns only

    @property
    def null_rate(self) -> float | None:
        return self.null_count / self.row_count if self.row_count else None


@dataclass(frozen=True)
class TableMeasurement:
    row_count: int | None
    row_count_is_estimate: bool
    size_bytes: int | None
    sample_fraction: float  # 1.0 = every row was read
    columns: tuple[ColumnMeasurement, ...]
    last_modified_at: Any = None  # a datetime when the database can tell; else None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KeyRef:
    """Columns of one table, e.g. the child side or parent side of a relationship."""

    schema: str
    table: str
    columns: tuple[str, ...]
    estimated_rows: int | None = None  # used to decide on sampling

    @property
    def ref(self) -> str:
        return f"{self.schema}.{self.table}"


@dataclass(frozen=True)
class InclusionResult:
    checked_rows: int  # child rows (non-null key) examined
    matched_rows: int  # of those, rows whose key exists in the parent
    sampled: bool

    @property
    def inclusion(self) -> float | None:
        return self.matched_rows / self.checked_rows if self.checked_rows else None


@dataclass(frozen=True)
class OrphanResult:
    checked_rows: int
    orphan_rows: int
    sample: list[dict[str, str]]  # identifiers of some offending rows (PK or key values)
    sampled: bool


@dataclass(frozen=True)
class QueryStat:
    query: str  # normalised statement text (constants replaced by placeholders)
    calls: int


class SourceAdapter(Protocol):
    def introspect(self) -> list[DiscoveredTable]: ...

    def profile(self, table: DiscoveredTable) -> TableMeasurement: ...

    # --- used by relationship discovery (Stage 1.4) ---------------------------------------
    def read_query_log(self, limit: int) -> list[QueryStat]: ...

    def value_inclusion(self, child: KeyRef, parent: KeyRef, max_rows: int) -> InclusionResult: ...

    def count_orphans(
        self, child: KeyRef, parent: KeyRef, row_ids: tuple[str, ...], sample_size: int
    ) -> OrphanResult: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
