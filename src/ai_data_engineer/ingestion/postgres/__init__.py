"""PostgreSQL adapter: catalog introspection and safe, read-only, in-database profiling."""

from ai_data_engineer.ingestion.postgres.adapter import APPLICATION_NAME, PostgresAdapter

__all__ = ["APPLICATION_NAME", "PostgresAdapter"]
