"""Structure of any SQL database through SQLAlchemy's inspector: schemas, tables and
views, columns, primary/unique keys, declared foreign keys, indexes and comments, plus
the database's own row-count estimates. Read-only catalog queries only."""

from typing import Any

from sqlalchemy import Connection, inspect
from sqlalchemy.engine.reflection import Inspector, ObjectKind
from sqlalchemy.exc import DBAPIError

from ai_data_engineer.graph.models import AssetKind
from ai_data_engineer.graph.versioning import AssetObservation, ColumnObservation
from ai_data_engineer.ingestion.base import DiscoveredTable
from ai_data_engineer.ingestion.settings import ScanSettings
from ai_data_engineer.ingestion.sql.dialects import DialectProfile
from ai_data_engineer.ingestion.sql.types import native_type, type_family

Key = tuple[str | None, str]


def read_structure(
    conn: Connection, profile: DialectProfile, settings: ScanSettings
) -> list[DiscoveredTable]:
    insp = inspect(conn)
    estimates = _estimates(conn, profile)
    namespace = conn.engine.url.database or ""
    found: list[DiscoveredTable] = []
    for schema in schemas_to_scan(insp, profile, settings):
        found += _schema(insp, conn, schema, namespace, estimates)
    return found


def schemas_to_scan(insp: Inspector, profile: DialectProfile, settings: ScanSettings) -> list[str]:
    default = insp.default_schema_name
    if profile.default_schema_only and not settings.include_schemas:
        candidates = [default] if default else []
    else:
        candidates = insp.get_schema_names()
    system = {s.lower() for s in profile.system_schemas}
    return [
        s
        for s in candidates
        if s.lower() not in system and not s.lower().startswith("pg_temp")
        and settings.includes_schema(s)
    ]  # fmt: skip


def _schema(
    insp: Inspector,
    conn: Connection,
    schema: str,
    namespace: str,
    estimates: dict[tuple[str, str], tuple[int | None, int | None]],
) -> list[DiscoveredTable]:
    kinds = {
        **{(schema, n): AssetKind.TABLE for n in insp.get_table_names(schema=schema)},
        **{(schema, n): AssetKind.VIEW for n in insp.get_view_names(schema=schema)},
    }
    if not kinds:
        return []
    any_kind = ObjectKind.TABLE | ObjectKind.VIEW
    columns = insp.get_multi_columns(schema=schema, kind=any_kind)
    pks = _multi(insp.get_multi_pk_constraint, schema)
    uniques = _multi(insp.get_multi_unique_constraints, schema)
    fks = _multi(insp.get_multi_foreign_keys, schema)
    indexes = _multi(insp.get_multi_indexes, schema)
    comments = _multi(insp.get_multi_table_comment, schema, any_kind)

    out = []
    for (schema_name, name), kind in sorted(kinds.items()):
        key: Key = (schema_name, name)
        cols = columns.get(key) or columns.get((None, name)) or []
        pk = tuple((_get(pks, key) or {}).get("constrained_columns") or ())
        rows, size = estimates.get((schema_name.lower(), name.lower()), (None, None))
        out.append(
            DiscoveredTable(
                observation=AssetObservation(
                    namespace=namespace,
                    schema_name=schema_name,
                    name=name,
                    kind=kind,
                    columns=tuple(
                        ColumnObservation(
                            name=c["name"],
                            native_type=native_type(c["type"], conn.dialect),
                            type_family=type_family(c["type"], conn.dialect),
                            is_nullable=bool(c.get("nullable", True)),
                            ordinal_position=i + 1,
                            default_expr=str(c["default"]) if c.get("default") else None,
                            comment=c.get("comment"),
                        )
                        for i, c in enumerate(cols)
                    ),
                    primary_key=pk,
                    indexes=tuple(_indexes(_get(indexes, key) or [], pk, _get(pks, key))),
                    unique_constraints=tuple(
                        {"name": u.get("name"), "columns": list(u["column_names"])}
                        for u in _get(uniques, key) or []
                    ),
                    foreign_keys=tuple(
                        {
                            "name": fk.get("name"),
                            "columns": list(fk["constrained_columns"]),
                            "ref_table": f"{fk.get('referred_schema') or schema_name}."
                            f"{fk['referred_table']}",
                            "ref_columns": list(fk["referred_columns"]),
                        }
                        for fk in _get(fks, key) or []
                    ),
                    comment=(_get(comments, key) or {}).get("text"),
                ),
                estimated_rows=rows,
                size_bytes=size,
            )
        )
    return out


def _indexes(
    reflected: list[dict[str, Any]], pk: tuple[str, ...], pk_info: dict[str, Any] | None
) -> list[dict[str, Any]]:
    out = [
        {
            "name": ix.get("name"),
            "columns": [c for c in ix.get("column_names") or [] if c],
            "unique": bool(ix.get("unique")),
            "primary": False,
        }
        for ix in reflected
    ]
    # Most inspectors leave the primary key's own index out; detection needs it to know
    # which columns are indexed.
    if pk and not any(ix["columns"] == list(pk) and ix["unique"] for ix in out):
        name = (pk_info or {}).get("name") or "primary"
        out.insert(0, {"name": name, "columns": list(pk), "unique": True, "primary": True})
    return out


def _multi(method: Any, schema: str, kind: Any = None) -> dict[Key, Any]:
    """Batch reflection; a dialect that can't (or a view without constraints) gives {}."""
    try:
        return dict(method(schema=schema, kind=kind) if kind else method(schema=schema))
    except (NotImplementedError, DBAPIError):
        return {}


def _get(found: dict[Key, Any], key: Key) -> Any:
    return found.get(key) or found.get((None, key[1]))


def _estimates(
    conn: Connection, profile: DialectProfile
) -> dict[tuple[str, str], tuple[int | None, int | None]]:
    """Row-count (and size) estimates keyed by lower-cased (schema, table)."""
    if profile.estimates_sql is None:
        return {}
    try:
        rows = conn.execute(profile.estimates_sql).all()
    except DBAPIError:
        return {}  # no permission: no sampling decisions, timeouts still protect
    return {
        (str(r[0]).lower(), str(r[1]).lower()): (
            int(r[2]) if r[2] is not None else None,
            int(r[3]) if r[3] is not None else None,
        )
        for r in rows
    }
