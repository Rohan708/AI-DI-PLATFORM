"""Map the column types SQLAlchemy reflects (for any dialect) to our type families."""

from typing import Any

from sqlalchemy import types as sqltypes
from sqlalchemy.engine import Dialect

from ai_data_engineer.graph.models import TypeFamily

# Large text types that can't be compared, counted distinct or min/max-ed on these
# databases (Oracle CLOB, SQL Server TEXT/NTEXT): treated as "other", like JSON.
_LOB_TEXT_DIALECTS = frozenset({"oracle", "mssql"})

# Checked in order: Boolean before Integer (some dialects subclass), Float before
# Numeric (Float is a Numeric).
_FAMILIES: tuple[tuple[type[Any], TypeFamily], ...] = (
    (sqltypes.Boolean, TypeFamily.BOOLEAN),
    (sqltypes.Integer, TypeFamily.INTEGER),
    (sqltypes.Float, TypeFamily.FLOAT),
    (sqltypes.Numeric, TypeFamily.DECIMAL),
    (sqltypes.DateTime, TypeFamily.TIMESTAMP),
    (sqltypes.Date, TypeFamily.DATE),
    (sqltypes.Time, TypeFamily.TIME),
    (sqltypes.Interval, TypeFamily.INTERVAL),
    (sqltypes.JSON, TypeFamily.JSON),
    (sqltypes.Uuid, TypeFamily.UUID),
    (sqltypes.ARRAY, TypeFamily.ARRAY),
    (sqltypes.String, TypeFamily.STRING),  # includes Text, Enum, Unicode
    (sqltypes._Binary, TypeFamily.BINARY),
)


def type_family(column_type: Any, dialect: Dialect) -> TypeFamily:
    if isinstance(column_type, sqltypes.Text) and dialect.name in _LOB_TEXT_DIALECTS:
        return TypeFamily.OTHER
    # Exact numerics by their scale: NUMBER(10, 0) is an integer, NUMBER(12, 2) a decimal.
    # (Oracle's NUMBER is both a Numeric and an Integer to SQLAlchemy, so check it first;
    # an unconstrained NUMBER can hold fractions, so it's a decimal.)
    if isinstance(column_type, sqltypes.Numeric) and not isinstance(column_type, sqltypes.Float):
        whole = getattr(column_type, "scale", None) == 0
        return TypeFamily.INTEGER if whole else TypeFamily.DECIMAL
    for cls, family in _FAMILIES:
        if isinstance(column_type, cls):
            return family
    return TypeFamily.OTHER


def native_type(column_type: Any, dialect: Dialect) -> str:
    """The type as the database spells it, e.g. ``VARCHAR(20)`` or ``NUMBER(12, 2)``."""
    try:
        return str(column_type.compile(dialect=dialect))
    except Exception:  # some reflected types can't be compiled back; the name will do
        return type(column_type).__name__.upper()
