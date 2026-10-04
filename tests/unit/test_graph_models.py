"""Model-level invariants that don't need a database."""

import uuid
from enum import StrEnum
from typing import cast

import pytest
from sqlalchemy import Dialect, Index

from ai_data_engineer.graph.models import (
    Base,
    FindingCategory,
    FindingStatus,
    Severity,
    StrEnumType,
    finding_fingerprint,
    relationship_signature,
)
from ai_data_engineer.graph.models import enums as enums_module
from ai_data_engineer.graph.models.base import ENUM_LENGTH
from ai_data_engineer.graph.versioning import RecordResult

DIALECT = cast(Dialect, None)  # StrEnumType ignores the dialect


def test_every_table_except_tenant_has_required_tenant_id() -> None:
    for name, table in Base.metadata.tables.items():
        if name == "tenant":
            continue
        assert "tenant_id" in table.c, name
        assert not table.c.tenant_id.nullable, name


def test_every_versioned_table_has_one_current_row_index() -> None:
    for name, table in Base.metadata.tables.items():
        if "valid_to" not in table.c:
            continue
        current = [
            ix
            for ix in table.indexes
            if isinstance(ix, Index)
            and ix.unique
            and ix.dialect_options["postgresql"]["where"] is not None
        ]
        assert current, f"{name} needs a partial unique index on its current row"


def test_all_enum_values_fit_the_column() -> None:
    enum_classes = [
        obj
        for obj in vars(enums_module).values()
        if isinstance(obj, type) and issubclass(obj, StrEnum)
    ]
    enum_classes.remove(StrEnum)
    for enum_cls in enum_classes:
        for member in enum_cls:
            assert member.value == member.value.lower()
            assert len(member.value) <= ENUM_LENGTH


def test_str_enum_type_round_trip() -> None:
    column_type = StrEnumType(Severity)
    assert column_type.process_bind_param(Severity.HIGH, DIALECT) == "high"
    assert column_type.process_bind_param("low", DIALECT) == "low"
    assert column_type.process_result_value("critical", DIALECT) is Severity.CRITICAL
    assert column_type.process_bind_param(None, DIALECT) is None
    with pytest.raises(ValueError, match="bogus"):
        column_type.process_bind_param("bogus", DIALECT)


def test_finding_categories_match_taxonomy() -> None:
    assert len(FindingCategory) == 9
    assert {s.value for s in FindingStatus} == {"open", "confirmed", "rejected", "resolved"}


def test_relationship_signature_is_order_insensitive() -> None:
    a, b, c, d = (uuid.uuid4() for _ in range(4))
    assert relationship_signature([(a, b), (c, d)]) == relationship_signature([(c, d), (a, b)])
    assert relationship_signature([(a, b)]) != relationship_signature([(b, a)])
    with pytest.raises(ValueError, match="at least one"):
        relationship_signature([])


def test_finding_fingerprint_is_deterministic() -> None:
    key = uuid.uuid4()
    assert finding_fingerprint("null_rate_spike", key) == finding_fingerprint(
        "null_rate_spike", key
    )
    assert finding_fingerprint("null_rate_spike", key) != finding_fingerprint("type_drift", key)
    assert len(finding_fingerprint("x")) == 64


def test_record_result_changed() -> None:
    key = uuid.uuid4()
    assert not RecordResult(asset_key=key).changed
    assert RecordResult(asset_key=key, columns_removed=["amount"]).changed
    assert RecordResult(asset_key=key, asset_reappeared=True).changed
