"""PostgreSQL adapter: catalog introspection and safe, read-only, in-database profiling."""

from lumida.ingestion.postgres.adapter import APPLICATION_NAME, PostgresAdapter

__all__ = ["APPLICATION_NAME", "PostgresAdapter"]
