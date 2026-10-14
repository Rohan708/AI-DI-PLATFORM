"""Declarative base, shared column types, and mixins for the metadata store."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Dialect, Double, ForeignKey, MetaData, String, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names, so Alembic migrations stay stable. Multi-column
# constraints and all indexes are named explicitly on the models.
NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
}

# Stored enum values are short strings; 32 chars leaves headroom.
ENUM_LENGTH = 32


def utcnow() -> datetime:
    """The only way the codebase gets "now": timezone-aware UTC."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        datetime: DateTime(timezone=True),
        uuid.UUID: UUID(as_uuid=True),
        float: Double(),
        dict[str, Any]: JSONB,
        list[str]: JSONB,
        list[dict[str, Any]]: JSONB,
    }


class StrEnumType[E: StrEnum](TypeDecorator[E]):
    """Stores a ``StrEnum`` as plain VARCHAR (not a Postgres enum type).

    Adding a new enum member then needs no database migration, which matters because
    finding categories, check names, and source kinds will keep growing.
    """

    impl = String(ENUM_LENGTH)
    cache_ok = True

    def __init__(self, enum_cls: type[E]) -> None:
        super().__init__()
        self.enum_cls = enum_cls

    def process_bind_param(self, value: E | str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return self.enum_cls(value).value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> E | None:
        if value is None:
            return None
        return self.enum_cls(value)


class TenantScoped:
    """Every table except ``tenant`` carries ``tenant_id`` (Postgres RLS comes later)."""

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"))
