"""The test lab is reproducible, its answer key is truthful, and every scenario does what
its answer-key entry claims."""

from collections.abc import Callable, Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from ai_data_engineer.db import create_db_engine
from ai_data_engineer.lab.answer_key import AnswerKey, write_answer_key
from ai_data_engineer.lab.runner import (
    PLANS,
    STAGE_ONE_SCENARIOS,
    answer_key,
    build_lab,
    inject,
    run_plan,
    tick,
)
from ai_data_engineer.lab.scenarios import ORPHAN_ID_OFFSET, SCENARIOS
from ai_data_engineer.lab.schema import TRUE_RELATIONSHIPS, LabSafetyError
from ai_data_engineer.lab.sizes import DEFAULT_START_DATE, SIZES
from ai_data_engineer.lab.state import load_state

SEED = 7
SIZE = "tiny"
CLEAN_DAYS = 3  # extra simulated days after build


def _engine(make_database: Callable[[], str]) -> Engine:
    return create_db_engine(make_database())


def _fingerprint(engine: Engine) -> tuple[Any, ...]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT (SELECT count(*) FROM shop.orders), "
                "(SELECT sum(total_amount) FROM shop.orders), "
                "(SELECT md5(string_agg(email, ',' ORDER BY id)) FROM shop.customers), "
                '(SELECT count(*) FROM legacy."INV_LINE"), '
                "(SELECT sum(revenue) FROM reporting.daily_sales)"
            )
        ).one()
    return tuple(row)


def _scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as conn:
        return conn.scalar(text(sql), params)


@pytest.fixture(scope="module")
def clean_lab(make_database: Callable[[], str]) -> Iterator[Engine]:
    engine = _engine(make_database)
    build_lab(engine, seed=SEED, size=SIZE)
    tick(engine, CLEAN_DAYS)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def planted_lab(make_database: Callable[[], str]) -> Iterator[tuple[Engine, AnswerKey]]:
    """Same seed as ``clean_lab``; identical until every scenario is injected before
    the last day."""
    engine = _engine(make_database)
    build_lab(engine, seed=SEED, size=SIZE)
    tick(engine, CLEAN_DAYS - 1)
    inject(engine, SCENARIOS)
    tick(engine, 1)
    yield engine, answer_key(engine)
    engine.dispose()


# --- reproducibility and truthfulness ---------------------------------------------------


def test_same_seed_gives_identical_data(
    clean_lab: Engine, make_database: Callable[[], str]
) -> None:
    twin = _engine(make_database)
    build_lab(twin, seed=SEED, size=SIZE)
    tick(twin, CLEAN_DAYS)
    assert _fingerprint(twin) == _fingerprint(clean_lab)
    twin.dispose()


def test_different_seed_gives_different_data(
    clean_lab: Engine, make_database: Callable[[], str]
) -> None:
    other = _engine(make_database)
    build_lab(other, seed=SEED + 1, size=SIZE)
    tick(other, CLEAN_DAYS)
    assert _fingerprint(other) != _fingerprint(clean_lab)
    other.dispose()


def test_declared_foreign_keys_match_answer_key(clean_lab: Engine) -> None:
    with clean_lab.connect() as conn:
        declared = {
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT tc.table_schema || '.' || tc.table_name, kcu.column_name, "
                    "ccu.table_schema || '.' || ccu.table_name, ccu.column_name "
                    "FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu "
                    "  ON kcu.constraint_name = tc.constraint_name "
                    " AND kcu.constraint_schema = tc.constraint_schema "
                    "JOIN information_schema.constraint_column_usage ccu "
                    "  ON ccu.constraint_name = tc.constraint_name "
                    " AND ccu.constraint_schema = tc.constraint_schema "
                    "WHERE tc.constraint_type = 'FOREIGN KEY' "
                    "AND tc.table_schema IN ('shop', 'legacy', 'reporting')"
                )
            )
        }
    expected = {
        (r.from_table, r.from_columns[0], r.to_table, r.to_columns[0])
        for r in TRUE_RELATIONSHIPS
        if r.declared
    }
    assert declared == expected


@pytest.mark.parametrize("relationship", TRUE_RELATIONSHIPS, ids=lambda r: r.from_table)
def test_every_true_relationship_holds_in_clean_data(clean_lab: Engine, relationship: Any) -> None:
    child, parent = _quoted(relationship.from_table), _quoted(relationship.to_table)
    not_null = " AND ".join(f'c."{col}" IS NOT NULL' for col in relationship.from_columns)
    join = " AND ".join(
        f'p."{p}" = c."{c}"'
        for c, p in zip(relationship.from_columns, relationship.to_columns, strict=True)
    )
    children = _scalar(clean_lab, f"SELECT count(*) FROM {child} c WHERE {not_null}")  # noqa: S608
    orphans = _scalar(
        clean_lab,
        f"SELECT count(*) FROM {child} c WHERE {not_null} "  # noqa: S608
        f"AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {join})",
    )
    assert children > 0
    assert orphans == 0


def test_days_advance_and_reporting_is_loaded(clean_lab: Engine) -> None:
    with clean_lab.connect() as conn:
        state = load_state(conn)
    days_simulated = SIZES[SIZE].history_days + CLEAN_DAYS
    assert state.current_day == DEFAULT_START_DATE + timedelta(days=days_simulated - 1)
    assert _scalar(clean_lab, "SELECT count(*) FROM reporting.daily_sales") == days_simulated
    assert _scalar(clean_lab, "SELECT count(*) FROM reporting.customer_summary") > 0
    assert _scalar(clean_lab, "SELECT count(*) FROM shop.shipments WHERE delivered_at IS NULL") > 0


def test_refuses_to_build_over_a_non_lab_database(make_database: Callable[[], str]) -> None:
    engine = _engine(make_database)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE public.important (id int)"))
    with pytest.raises(LabSafetyError):
        build_lab(engine, seed=SEED, size=SIZE)
    assert _scalar(engine, "SELECT count(*) FROM public.important") == 0  # untouched
    engine.dispose()


# --- every scenario does what its answer key says ---------------------------------------


def test_answer_key_covers_every_scenario(planted_lab: tuple[Engine, AnswerKey]) -> None:
    _, key = planted_lab
    assert {a.scenario for a in key.anomalies} == set(SCENARIOS)
    assert all(a.effective_day == key.current_day for a in key.anomalies)


def test_structural_scenarios(planted_lab: tuple[Engine, AnswerKey]) -> None:
    engine, _ = planted_lab
    assert (
        _scalar(
            engine,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'shop' AND table_name = 'products' "
            "AND column_name = 'weight_grams'",
        )
        == 0
    )
    assert (
        _scalar(
            engine,
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = 'shop' AND table_name = 'orders' AND column_name = 'channel'",
        )
        == 50
    )
    assert (
        _scalar(
            engine,
            "SELECT count(*) FROM information_schema.table_constraints "
            "WHERE table_schema = 'legacy' AND table_name = 'INV_LINE' "
            "AND constraint_type = 'PRIMARY KEY'",
        )
        == 0
    )


def test_relational_scenarios(planted_lab: tuple[Engine, AnswerKey]) -> None:
    engine, key = planted_lab
    orphans = _scalar(
        engine, "SELECT count(*) FROM shop.orders WHERE cust_no >= :offset", offset=ORPHAN_ID_OFFSET
    )
    assert orphans == _details(key, "orphan_orders")["rows"] > 0
    duplicated_emails = _scalar(
        engine,
        "SELECT count(*) FROM (SELECT lower(email) FROM shop.customers "
        "WHERE email IS NOT NULL GROUP BY lower(email) HAVING count(*) > 1) d",
    )
    assert duplicated_emails == _details(key, "duplicate_customers")["rows"] > 0


def test_column_value_scenarios(planted_lab: tuple[Engine, AnswerKey]) -> None:
    engine, key = planted_lab
    null_postcodes = _scalar(
        engine, "SELECT count(*) FROM shop.addresses WHERE postal_code IS NULL"
    )
    assert null_postcodes == _details(key, "null_spike")["rows"] > 0
    variants = _scalar(
        engine,
        "SELECT count(*) FROM shop.addresses WHERE country IN ('USA', 'United States')",
    )
    assert variants == _details(key, "country_variants")["rows"] > 0
    assert _scalar(engine, "SELECT min(quantity) FROM shop.order_items") < 0


def test_etl_fault_scenarios(planted_lab: tuple[Engine, AnswerKey], clean_lab: Engine) -> None:
    engine, key = planted_lab
    day = key.current_day
    loaded = "SELECT count(*) FROM reporting.daily_sales WHERE sales_date = :day"
    assert _scalar(clean_lab, loaded, day=day) == 1
    assert _scalar(engine, loaded, day=day) == 0  # skipped_load

    orders_that_day = (
        "SELECT count(*) FROM shop.orders WHERE (order_date AT TIME ZONE 'UTC')::date = :day"
    )
    assert _scalar(engine, orders_that_day, day=day) < _scalar(clean_lab, orders_that_day, day=day)
    with engine.connect() as conn:
        assert load_state(conn).pending_faults == {}  # consumed by the simulated day


def test_business_rule_scenarios(planted_lab: tuple[Engine, AnswerKey]) -> None:
    engine, key = planted_lab
    early = _scalar(
        engine,
        "SELECT count(*) FROM shop.shipments s JOIN shop.orders o ON o.order_id = s.ord_id "
        "WHERE s.shipped_at < o.order_date",
    )
    assert early == _details(key, "ship_before_order")["rows"] > 0
    mismatched = _scalar(
        engine,
        'SELECT count(*) FROM legacy."INV_HDR" h WHERE h."TOTAL_AMT" <> '
        '(SELECT sum(l."LINE_AMT") FROM legacy."INV_LINE" l WHERE l."INV_NO" = h."INV_NO")',
    )
    assert mismatched == _details(key, "invoice_total_mismatch")["rows"] > 0


def test_answer_key_files_are_written(
    planted_lab: tuple[Engine, AnswerKey], tmp_path: Path
) -> None:
    _, key = planted_lab
    json_path, md_path = write_answer_key(key, tmp_path, "round")
    assert json_path.exists()
    markdown = md_path.read_text(encoding="utf-8")
    assert "orphan_orders" in markdown
    assert "Normal patterns" in markdown


def test_run_plan_calls_the_hook_once_per_simulated_day(
    make_database: Callable[[], str],
) -> None:
    engine = _engine(make_database)
    scanned: list[date] = []
    key = run_plan(engine, "quick", seed=SEED, size=SIZE, on_day_end=scanned.append)
    plan = PLANS["quick"]
    assert len(scanned) == plan.baseline_days + plan.days_after
    assert {a.scenario for a in key.anomalies} == set(STAGE_ONE_SCENARIOS)
    engine.dispose()


# --- helpers ----------------------------------------------------------------------------


def _quoted(table: str) -> str:
    schema, name = table.split(".")
    return f'"{schema}"."{name}"'


def _details(key: AnswerKey, scenario: str) -> dict[str, Any]:
    return next(a.details for a in key.anomalies if a.scenario == scenario)


def test_rebuilding_an_existing_lab_is_allowed(make_database: Callable[[], str]) -> None:
    """The lab's own pg_stat_statements views in public must not trip the safety guard."""
    engine = _engine(make_database)
    build_lab(engine, seed=SEED, size=SIZE)
    state = build_lab(engine, seed=SEED, size=SIZE)  # second build over the first
    assert state.current_day == DEFAULT_START_DATE + timedelta(days=SIZES[SIZE].history_days - 1)
    engine.dispose()
