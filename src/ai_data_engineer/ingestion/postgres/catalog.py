"""Read structure from the Postgres system catalog. Touches no table data."""

from collections import defaultdict
from typing import Any

from sqlalchemy import Connection, text

from ai_data_engineer.graph.models import AssetKind
from ai_data_engineer.graph.versioning import AssetObservation, ColumnObservation
from ai_data_engineer.ingestion.base import DiscoveredTable
from ai_data_engineer.ingestion.postgres.types import postgres_type_family
from ai_data_engineer.ingestion.settings import ScanSettings

# Rough bytes per row, only used when Postgres has never estimated a table's size.
FALLBACK_BYTES_PER_ROW = 100

_RELKIND: dict[str, AssetKind] = {
    "r": AssetKind.TABLE,
    "p": AssetKind.TABLE,  # partitioned parent; its partitions are skipped
    "v": AssetKind.VIEW,
    "m": AssetKind.MATERIALIZED_VIEW,
    "f": AssetKind.FOREIGN_TABLE,
}

_RELATIONS_SQL = text("""
SELECT c.oid, n.nspname AS schema_name, c.relname AS name, c.relkind,
       c.reltuples::bigint AS reltuples,
       pg_relation_size(c.oid) AS heap_bytes,
       pg_total_relation_size(c.oid) AS size_bytes,
       obj_description(c.oid, 'pg_class') AS comment,
       current_database() AS database_name
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND NOT c.relispartition
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname NOT LIKE 'pg\\_temp%'
  AND n.nspname NOT LIKE 'pg\\_toast%'
  -- skip objects owned by extensions (e.g. the pg_stat_statements views): not customer data
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend dep
      WHERE dep.classid = 'pg_class'::regclass AND dep.objid = c.oid AND dep.deptype = 'e'
  )
ORDER BY n.nspname, c.relname
""")

_COLUMNS_SQL = text("""
SELECT a.attrelid AS oid, a.attname AS name, a.attnum AS position,
       format_type(a.atttypid, a.atttypmod) AS native_type,
       t.typname AS type_name, t.typcategory AS type_category,
       NOT a.attnotnull AS is_nullable,
       pg_get_expr(d.adbin, d.adrelid) AS default_expr,
       col_description(a.attrelid, a.attnum) AS comment
FROM pg_attribute a
JOIN pg_type t ON t.oid = a.atttypid
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = ANY(CAST(:oids AS oid[])) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attrelid, a.attnum
""")

_CONSTRAINTS_SQL = text("""
SELECT con.conrelid AS oid, con.conname AS name, con.contype AS kind,
       ARRAY(SELECT att.attname FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)
             JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum
             ORDER BY k.ord) AS columns,
       rn.nspname AS ref_schema, rc.relname AS ref_table,
       ARRAY(SELECT att.attname FROM unnest(con.confkey) WITH ORDINALITY k(attnum, ord)
             JOIN pg_attribute att ON att.attrelid = con.confrelid AND att.attnum = k.attnum
             ORDER BY k.ord) AS ref_columns
FROM pg_constraint con
LEFT JOIN pg_class rc ON rc.oid = con.confrelid
LEFT JOIN pg_namespace rn ON rn.oid = rc.relnamespace
WHERE con.conrelid = ANY(CAST(:oids AS oid[])) AND con.contype IN ('p', 'u', 'f')
ORDER BY con.conrelid, con.conname
""")

_INDEXES_SQL = text("""
SELECT i.indrelid AS oid, ic.relname AS name, i.indisunique AS is_unique,
       i.indisprimary AS is_primary,
       ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true)
             FROM generate_series(1, i.indnkeyatts) AS k) AS columns
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
WHERE i.indrelid = ANY(CAST(:oids AS oid[]))
ORDER BY i.indrelid, ic.relname
""")

# Cumulative activity counters, kept as profile properties (freshness/volume signals).
_ACTIVITY_SQL = text("""
SELECT relid AS oid, n_tup_ins, n_tup_upd, n_tup_del, n_live_tup,
       last_analyze, last_autoanalyze
FROM pg_stat_user_tables
WHERE relid = ANY(CAST(:oids AS oid[]))
""")


def read_structure(conn: Connection, settings: ScanSettings) -> list[DiscoveredTable]:
    relations = [
        r for r in conn.execute(_RELATIONS_SQL).all() if settings.includes_schema(r.schema_name)
    ]
    if not relations:
        return []
    oids = [int(r.oid) for r in relations]
    params = {"oids": oids}

    columns: dict[int, list[ColumnObservation]] = defaultdict(list)
    for c in conn.execute(_COLUMNS_SQL, params):
        columns[int(c.oid)].append(
            ColumnObservation(
                name=c.name,
                native_type=c.native_type,
                type_family=postgres_type_family(c.type_name, c.type_category),
                is_nullable=c.is_nullable,
                ordinal_position=int(c.position),
                default_expr=c.default_expr,
                comment=c.comment,
            )
        )

    primary_key: dict[int, tuple[str, ...]] = {}
    uniques: dict[int, list[dict[str, Any]]] = defaultdict(list)
    foreign_keys: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for con in conn.execute(_CONSTRAINTS_SQL, params):
        oid = int(con.oid)
        if con.kind == "p":
            primary_key[oid] = tuple(con.columns)
        elif con.kind == "u":
            uniques[oid].append({"name": con.name, "columns": list(con.columns)})
        else:
            foreign_keys[oid].append(
                {
                    "name": con.name,
                    "columns": list(con.columns),
                    "ref_table": f"{con.ref_schema}.{con.ref_table}",
                    "ref_columns": list(con.ref_columns),
                }
            )

    indexes: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for ix in conn.execute(_INDEXES_SQL, params):
        indexes[int(ix.oid)].append(
            {
                "name": ix.name,
                "columns": list(ix.columns),
                "unique": ix.is_unique,
                "primary": ix.is_primary,
            }
        )

    discovered = []
    for r in relations:
        oid = int(r.oid)
        discovered.append(
            DiscoveredTable(
                observation=AssetObservation(
                    namespace=r.database_name,
                    schema_name=r.schema_name,
                    name=r.name,
                    kind=_RELKIND[r.relkind],
                    columns=tuple(columns.get(oid, [])),
                    primary_key=primary_key.get(oid, ()),
                    indexes=tuple(indexes.get(oid, [])),
                    unique_constraints=tuple(uniques.get(oid, [])),
                    foreign_keys=tuple(foreign_keys.get(oid, [])),
                    comment=r.comment,
                ),
                estimated_rows=_estimated_rows(r.reltuples, r.heap_bytes),
                size_bytes=int(r.size_bytes) if r.size_bytes is not None else None,
                native_id=oid,
            )
        )
    return discovered


def read_activity(conn: Connection, oid: int) -> dict[str, Any]:
    row = conn.execute(_ACTIVITY_SQL, {"oids": [oid]}).mappings().first()
    if row is None:
        return {}
    return {
        key: (value.isoformat() if hasattr(value, "isoformat") else value)
        for key, value in row.items()
        if key != "oid"
    }


def _estimated_rows(reltuples: int | None, heap_bytes: int | None) -> int | None:
    # reltuples is -1 when Postgres has never analysed the table.
    if reltuples is not None and reltuples >= 0:
        return int(reltuples)
    if heap_bytes is None:
        return None
    return int(heap_bytes) // FALLBACK_BYTES_PER_ROW
