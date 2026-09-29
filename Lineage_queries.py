"""
Lineage traversal — upstream (root-cause) and downstream (impact analysis).

Both use a recursive CTE over edge_derives_from (column-level lineage).
This is the single most-used query pattern in the whole platform, so it's
worth getting right and fast (indexed) before anything else.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session


UPSTREAM_QUERY = text("""
-- Given a column, find everything that feeds into it, transitively.
-- Used by: root-cause investigation ("what could have caused this?")
WITH RECURSIVE upstream AS (
    SELECT
        source_column_id,
        target_column_id,
        produced_by_job_id,
        1 AS depth
    FROM edge_derives_from
    WHERE target_column_id = :column_id

    UNION ALL

    SELECT
        e.source_column_id,
        e.target_column_id,
        e.produced_by_job_id,
        u.depth + 1
    FROM edge_derives_from e
    JOIN upstream u ON e.target_column_id = u.source_column_id
    WHERE u.depth < :max_depth  -- guard against cycles / runaway traversal
)
SELECT DISTINCT
    ac.id AS column_id,
    ac.column_name,
    a.database_name,
    a.schema_name,
    a.table_name,
    u.produced_by_job_id,
    u.depth
FROM upstream u
JOIN asset_column ac ON ac.id = u.source_column_id
JOIN asset a ON a.id = ac.asset_id
ORDER BY u.depth;
""")


DOWNSTREAM_QUERY = text("""
-- Given a column, find everything that depends on it, transitively.
-- Used by: impact analysis ("what breaks if this is wrong?")
WITH RECURSIVE downstream AS (
    SELECT
        source_column_id,
        target_column_id,
        produced_by_job_id,
        1 AS depth
    FROM edge_derives_from
    WHERE source_column_id = :column_id

    UNION ALL

    SELECT
        e.source_column_id,
        e.target_column_id,
        e.produced_by_job_id,
        d.depth + 1
    FROM edge_derives_from e
    JOIN downstream d ON e.source_column_id = d.target_column_id
    WHERE d.depth < :max_depth
)
SELECT DISTINCT
    ac.id AS column_id,
    ac.column_name,
    a.database_name,
    a.schema_name,
    a.table_name,
    d.produced_by_job_id,
    d.depth
FROM downstream d
JOIN asset_column ac ON ac.id = d.target_column_id
JOIN asset a ON a.id = ac.asset_id
ORDER BY d.depth;
""")


DOWNSTREAM_DASHBOARDS_QUERY = text("""
-- Extend impact analysis all the way to dashboards, via the downstream
-- assets found above. Run this second, feeding in the asset_ids from
-- the downstream column query.
SELECT DISTINCT
    d.id AS dashboard_id,
    d.dashboard_name,
    d.url
FROM edge_feeds f
JOIN dashboard d ON d.id = f.dashboard_id
WHERE f.asset_id = ANY(:asset_ids);
""")


def get_upstream_lineage(session: Session, column_id: str, max_depth: int = 10):
    """Root-cause investigation entry point: trace a problem backward."""
    return session.execute(
        UPSTREAM_QUERY, {"column_id": column_id, "max_depth": max_depth}
    ).mappings().all()


def get_downstream_lineage(session: Session, column_id: str, max_depth: int = 10):
    """Impact analysis entry point: trace a problem forward."""
    return session.execute(
        DOWNSTREAM_QUERY, {"column_id": column_id, "max_depth": max_depth}
    ).mappings().all()


def get_affected_dashboards(session: Session, asset_ids: list[str]):
    return session.execute(
        DOWNSTREAM_DASHBOARDS_QUERY, {"asset_ids": asset_ids}
    ).mappings().all()