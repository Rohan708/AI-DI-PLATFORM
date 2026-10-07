"""Adapter pieces that don't need a database: type mapping, settings, connection refs,
profiling-SQL generation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_data_engineer.graph.models import AssetKind, TypeFamily
from ai_data_engineer.graph.versioning import AssetObservation, ColumnObservation
from ai_data_engineer.ingestion.base import ColumnMeasurement, DiscoveredTable
from ai_data_engineer.ingestion.connections import ConnectionRefError, resolve_connection_url
from ai_data_engineer.ingestion.postgres.profiling import build_profile_query, quote_ident
from ai_data_engineer.ingestion.postgres.types import postgres_type_family
from ai_data_engineer.ingestion.settings import DEFAULT_STATEMENT_TIMEOUT_MS, ScanSettings


@pytest.mark.parametrize(
    ("type_name", "category", "family"),
    [
        ("int4", "N", TypeFamily.INTEGER),
        ("int8", "N", TypeFamily.INTEGER),
        ("numeric", "N", TypeFamily.DECIMAL),
        ("float8", "N", TypeFamily.FLOAT),
        ("varchar", "S", TypeFamily.STRING),
        ("bpchar", "S", TypeFamily.STRING),  # char(n)
        ("text", "S", TypeFamily.STRING),
        ("my_enum", "E", TypeFamily.STRING),
        ("timestamptz", "D", TypeFamily.TIMESTAMP),
        ("date", "D", TypeFamily.DATE),
        ("bool", "B", TypeFamily.BOOLEAN),
        ("jsonb", "U", TypeFamily.JSON),
        ("uuid", "U", TypeFamily.UUID),
        ("_int4", "A", TypeFamily.ARRAY),
        ("tsvector", "U", TypeFamily.OTHER),
    ],
)
def test_postgres_type_family(type_name: str, category: str, family: TypeFamily) -> None:
    assert postgres_type_family(type_name, category) is family


# --- settings -----------------------------------------------------------------------------


def test_scan_settings_defaults_and_validation() -> None:
    settings = ScanSettings()
    assert settings.statement_timeout_ms == DEFAULT_STATEMENT_TIMEOUT_MS
    assert settings.allow_value_samples
    with pytest.raises(ValidationError):
        ScanSettings.model_validate({"statment_timeout_ms": 5})  # typo must fail
    with pytest.raises(ValidationError):
        ScanSettings(statement_timeout_ms=0)


def test_schema_include_exclude() -> None:
    everything = ScanSettings(exclude_schemas=("aide_lab",))
    assert everything.includes_schema("shop")
    assert not everything.includes_schema("aide_lab")
    only_shop = ScanSettings(include_schemas=("shop", "legacy"), exclude_schemas=("legacy",))
    assert only_shop.includes_schema("shop")
    assert not only_shop.includes_schema("legacy")  # exclusion wins
    assert not only_shop.includes_schema("reporting")


# --- connection refs ------------------------------------------------------------------------


def test_connection_ref_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIDE_TEST_DB", "postgresql+psycopg://u:p@h/db")
    assert resolve_connection_url("AIDE_TEST_DB", env_file=tmp_path / ".env") == (
        "postgresql+psycopg://u:p@h/db"
    )


def test_connection_ref_from_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AIDE_TEST_DB", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("AIDE_TEST_DB=postgresql+psycopg://from-file/db\n", encoding="utf-8")
    assert resolve_connection_url("AIDE_TEST_DB", env_file=env_file).endswith("from-file/db")


def test_missing_connection_ref_fails_clearly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AIDE_NOPE", raising=False)
    with pytest.raises(ConnectionRefError, match="AIDE_NOPE"):
        resolve_connection_url("AIDE_NOPE", env_file=tmp_path / ".env")
    with pytest.raises(ConnectionRefError):
        resolve_connection_url(None)


# --- profiling SQL --------------------------------------------------------------------------


def _table(estimated_rows: int | None = 100) -> DiscoveredTable:
    return DiscoveredTable(
        observation=AssetObservation(
            namespace="db",
            schema_name="Legacy",
            name="CUST",
            kind=AssetKind.TABLE,
            columns=(
                ColumnObservation("id", "integer", TypeFamily.INTEGER, False, 1),
                ColumnObservation("Odd:Name", "text", TypeFamily.STRING, True, 2),
                ColumnObservation("payload", "jsonb", TypeFamily.JSON, True, 3),
                ColumnObservation("active", "boolean", TypeFamily.BOOLEAN, True, 4),
            ),
        ),
        estimated_rows=estimated_rows,
        size_bytes=None,
    )


def test_identifiers_are_quoted_and_colons_escaped() -> None:
    assert quote_ident("CUSTID") == '"CUSTID"'
    assert quote_ident('we"ird') == '"we""ird"'
    assert quote_ident("a:b") == '"a\\:b"'


def test_profile_query_measures_each_family_appropriately() -> None:
    plan = build_profile_query(_table(), ScanSettings())
    sql = plan.sql
    assert 'FROM "Legacy"."CUST"' in sql
    assert "TABLESAMPLE" not in sql
    assert plan.sample_fraction == 1.0
    assert 'avg("id")::float8 AS c0_mean' in sql  # numeric stats for integers
    assert 'avg(length("Odd\\:Name"))' in sql  # string length
    assert "c2_distinct" not in sql  # no distinct count for json
    assert "c3_min" not in sql  # booleans have no min/max
    assert "c1_min" in sql  # text min/max allowed by default


def test_profile_query_without_value_samples_skips_text_extremes() -> None:
    sql = build_profile_query(_table(), ScanSettings(allow_value_samples=False)).sql
    assert "c1_min" not in sql
    assert "c0_min" in sql  # numbers are not "values" for privacy purposes


def test_large_tables_are_sampled() -> None:
    plan = build_profile_query(
        _table(estimated_rows=10_000), ScanSettings(sample_row_threshold=100)
    )
    assert plan.sampled
    assert plan.sample_fraction == pytest.approx(0.01)
    assert "TABLESAMPLE SYSTEM (1.000000) REPEATABLE (0)" in plan.sql


def test_null_rate_handles_empty_tables() -> None:
    empty = ColumnMeasurement("x", 0, 0, 0, False, None, None)
    assert empty.null_rate is None
    assert ColumnMeasurement("x", 200, 50, 10, False, None, None).null_rate == 0.25
