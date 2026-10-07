"""Remember query-log joins across runs (``query_join`` table).

``pg_stat_statements`` counts reset when Postgres restarts, so evidence seen yesterday can
be gone today. Each discovery run merges what the query log shows now into the stored
pairs (keeping the largest call count ever seen) and returns the remembered totals.
"""

import uuid
from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.discovery.catalog import Catalog
from ai_data_engineer.discovery.querylog import ColumnRef
from ai_data_engineer.graph.models import QueryJoin


def remember_joins(
    session: Session, catalog: Catalog, seen: Counter[frozenset[ColumnRef]], now: datetime
) -> Counter[frozenset[ColumnRef]]:
    keys = {
        (table.schema_name, table.name, column.name): column.column_key
        for table in catalog.tables.values()
        for column in table.columns.values()
    }
    refs = {key: ref for ref, key in keys.items()}
    stored = {
        (row.left_column_key, row.right_column_key): row
        for row in session.scalars(
            select(QueryJoin).where(QueryJoin.data_source_id == catalog.source.id)
        )
    }

    for pair, calls in seen.items():
        if len(pair) != 2:
            continue
        a, b = (keys.get(ref) for ref in pair)
        if a is None or b is None:
            continue  # a column outside the scanned schemas
        left, right = _ordered(a, b)
        row = stored.get((left, right))
        if row is None:
            row = QueryJoin(
                tenant_id=catalog.source.tenant_id,
                data_source_id=catalog.source.id,
                left_column_key=left,
                right_column_key=right,
                max_calls=calls,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(row)
            stored[(left, right)] = row
        else:
            row.max_calls = max(row.max_calls, calls)
            row.last_seen_at = max(row.last_seen_at, now)
    session.flush()

    remembered: Counter[frozenset[ColumnRef]] = Counter()
    for (left, right), row in stored.items():
        if left in refs and right in refs:
            remembered[frozenset({refs[left], refs[right]})] = row.max_calls
    return remembered


def _ordered(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if str(a) <= str(b) else (b, a)
