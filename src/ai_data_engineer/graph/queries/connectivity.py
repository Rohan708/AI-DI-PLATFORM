"""Which tables are connected to a table through relationships (impact analysis).

A recursive CTE walks current (non-closed) relationships, either direction:
- "parents":  tables this one references   (orders -> customers)
- "children": tables that reference this one (customers -> orders)
- "both":     everything connected

Loops are safe (each path remembers the tables it visited) and depth is capped.
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from ai_data_engineer.graph.models import RelationshipStatus

Direction = Literal["both", "parents", "children"]

DEFAULT_MAX_DEPTH = 3
MAX_TRAVERSAL_DEPTH = 10  # hard cap, protects against runaway traversal on huge schemas

_CONNECTED_ASSETS_SQL = text("""
WITH RECURSIVE edges AS (
    SELECT from_asset_key AS src, to_asset_key AS dst
    FROM relationship
    WHERE tenant_id = :tenant_id
      AND valid_to IS NULL
      AND status = ANY(:statuses)
      AND :follow_parents
    UNION
    SELECT to_asset_key AS src, from_asset_key AS dst
    FROM relationship
    WHERE tenant_id = :tenant_id
      AND valid_to IS NULL
      AND status = ANY(:statuses)
      AND :follow_children
),
walk (asset_key, depth, path) AS (
    SELECT CAST(:start AS uuid), 0, ARRAY[CAST(:start AS uuid)]
    UNION ALL
    SELECT e.dst, w.depth + 1, w.path || e.dst
    FROM walk w
    JOIN edges e ON e.src = w.asset_key
    WHERE w.depth < :max_depth
      AND NOT e.dst = ANY(w.path)
)
SELECT a.asset_key, a.namespace, a.schema_name, a.name, MIN(w.depth) AS depth
FROM walk w
JOIN asset a ON a.asset_key = w.asset_key
WHERE w.depth > 0
  AND a.tenant_id = :tenant_id
GROUP BY a.asset_key, a.namespace, a.schema_name, a.name
ORDER BY depth, a.schema_name, a.name
""")


@dataclass(frozen=True)
class ConnectedAsset:
    asset_key: uuid.UUID
    namespace: str
    schema_name: str
    name: str
    depth: int  # shortest number of relationship hops from the start table


def get_connected_assets(
    session: Session,
    tenant_id: uuid.UUID,
    asset_key: uuid.UUID,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    direction: Direction = "both",
    include_proposed: bool = False,
) -> list[ConnectedAsset]:
    """Tables reachable from ``asset_key`` within ``max_depth`` hops.

    Only confirmed relationships are followed unless ``include_proposed`` is set.
    Rejected relationships are never followed.
    """
    if not 1 <= max_depth <= MAX_TRAVERSAL_DEPTH:
        raise ValueError(f"max_depth must be between 1 and {MAX_TRAVERSAL_DEPTH}")
    statuses = [RelationshipStatus.CONFIRMED.value]
    if include_proposed:
        statuses.append(RelationshipStatus.PROPOSED.value)

    rows = session.execute(
        _CONNECTED_ASSETS_SQL,
        {
            "tenant_id": tenant_id,
            "start": asset_key,
            "statuses": statuses,
            "follow_parents": direction in ("both", "parents"),
            "follow_children": direction in ("both", "children"),
            "max_depth": max_depth,
        },
    ).all()
    return [
        ConnectedAsset(
            asset_key=row.asset_key,
            namespace=row.namespace,
            schema_name=row.schema_name,
            name=row.name,
            depth=row.depth,
        )
        for row in rows
    ]
