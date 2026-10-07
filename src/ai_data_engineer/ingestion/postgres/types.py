"""Map Postgres types to database-neutral type families."""

from ai_data_engineer.graph.models import TypeFamily

_BY_NAME: dict[str, TypeFamily] = {
    "int2": TypeFamily.INTEGER,
    "int4": TypeFamily.INTEGER,
    "int8": TypeFamily.INTEGER,
    "oid": TypeFamily.INTEGER,
    "numeric": TypeFamily.DECIMAL,
    "money": TypeFamily.DECIMAL,
    "float4": TypeFamily.FLOAT,
    "float8": TypeFamily.FLOAT,
    "bool": TypeFamily.BOOLEAN,
    "date": TypeFamily.DATE,
    "timestamp": TypeFamily.TIMESTAMP,
    "timestamptz": TypeFamily.TIMESTAMP,
    "time": TypeFamily.TIME,
    "timetz": TypeFamily.TIME,
    "interval": TypeFamily.INTERVAL,
    "bytea": TypeFamily.BINARY,
    "json": TypeFamily.JSON,
    "jsonb": TypeFamily.JSON,
    "uuid": TypeFamily.UUID,
}

# pg_type.typcategory codes: A = array, S = string, E = enum.
_BY_CATEGORY: dict[str, TypeFamily] = {
    "A": TypeFamily.ARRAY,
    "S": TypeFamily.STRING,
    "E": TypeFamily.STRING,
}


def postgres_type_family(type_name: str, type_category: str) -> TypeFamily:
    """``type_name`` is ``pg_type.typname`` (e.g. ``int4``, ``varchar``, ``_text``)."""
    if type_name in _BY_NAME:
        return _BY_NAME[type_name]
    return _BY_CATEGORY.get(type_category, TypeFamily.OTHER)
