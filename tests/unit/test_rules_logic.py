"""Rule format, validation, SQL compilation, the LLM plumbing and proposal parsing:
everything in Stage 2 that needs no database. No real LLM calls (urlopen is faked)."""

import io
import json
import urllib.request
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from ai_data_engineer.config import Settings
from ai_data_engineer.discovery.catalog import Catalog, ColumnInfo, TableInfo
from ai_data_engineer.graph.models import DataSource, RuleStatus, TypeFamily
from ai_data_engineer.ingestion.postgres.rules import compile_rule
from ai_data_engineer.lab.rule_scoring import found_rule, score_rules
from ai_data_engineer.lab.schema import HIDDEN_RULES
from ai_data_engineer.reasoning import llm as llm_module
from ai_data_engineer.reasoning.context import schema_context
from ai_data_engineer.reasoning.llm import (
    GeminiClient,
    LLMError,
    LLMNotConfiguredError,
    llm_from_settings,
    parse_gemini_reply,
)
from ai_data_engineer.reasoning.propose import parse_rules_reply
from ai_data_engineer.rules.settings import RuleSettings
from ai_data_engineer.rules.spec import Link, describe, parse_spec, signature, validate

SHIPPED = {
    "kind": "compare_columns", "table": "shop.shipments", "column": "shipped_at", "op": ">=",
    "other_column": "order_date",
    "via": {"parent_table": "shop.orders", "on": [["ord_id", "order_id"]]},
}  # fmt: skip
TOTAL = {
    "kind": "sum_matches", "table": "legacy.INV_HDR", "column": "TOTAL_AMT",
    "child_table": "legacy.INV_LINE", "child_column": "LINE_AMT", "on": [["INV_NO", "INV_NO"]],
}  # fmt: skip
QTY = {"kind": "compare_constant", "table": "shop.orders", "column": "qty", "op": ">", "value": 0}
LINKS: list[Link] = [
    ("shop.shipments", ("ord_id",), "shop.orders", ("order_id",)),
    ("legacy.INV_LINE", ("INV_NO",), "legacy.INV_HDR", ("INV_NO",)),
]


def _col(name: str, family: TypeFamily, **kw: Any) -> ColumnInfo:
    return ColumnInfo(uuid.uuid4(), name, family, family.value, **kw)


def _table(ref: str, *columns: ColumnInfo) -> TableInfo:
    schema, name = ref.split(".")
    return TableInfo(
        asset_key=uuid.uuid4(), schema_name=schema, name=name,
        columns={c.name: c for c in columns}, primary_key=(), unique_keys=(),
        historical_keys=(), foreign_keys=(),
    )  # fmt: skip


def _catalog() -> Catalog:
    catalog = Catalog(source=DataSource(name="t"))
    for table in (
        _table(
            "shop.shipments",
            _col("ord_id", TypeFamily.INTEGER),
            _col("shipped_at", TypeFamily.TIMESTAMP),
            _col("carrier", TypeFamily.STRING),
        ),
        _table(
            "shop.orders",
            _col("order_id", TypeFamily.INTEGER),
            _col("order_date", TypeFamily.TIMESTAMP),
            _col("qty", TypeFamily.INTEGER, row_count=10, null_count=1, min_repr="1",
                 max_repr="9"),
            _col("email", TypeFamily.STRING, min_repr="a@x.io", max_repr="z@x.io"),
        ),
        _table(
            "legacy.INV_HDR",
            _col("INV_NO", TypeFamily.INTEGER),
            _col("TOTAL_AMT", TypeFamily.DECIMAL),
        ),
        _table(
            "legacy.INV_LINE",
            _col("INV_NO", TypeFamily.INTEGER),
            _col("LINE_AMT", TypeFamily.DECIMAL),
        ),
    ):
        catalog.tables[table.ref] = table
    return catalog


# --- the rule format -----------------------------------------------------------------------


def test_rules_parse_describe_and_have_stable_signatures() -> None:
    shipped, total, qty = parse_spec(SHIPPED), parse_spec(TOTAL), parse_spec(QTY)
    assert describe(shipped) == (
        "shop.shipments.shipped_at >= shop.orders.order_date (via ord_id -> order_id)"
    )
    assert describe(total).startswith("legacy.INV_HDR.TOTAL_AMT = sum(legacy.INV_LINE.LINE_AMT)")
    assert describe(qty) == "shop.orders.qty > 0"
    reordered = parse_spec(dict(reversed(list(SHIPPED.items()))))
    assert signature(reordered) == signature(shipped)
    assert signature(parse_spec({**QTY, "op": ">="})) != signature(qty)


@pytest.mark.parametrize(
    "bad",
    [
        {**QTY, "op": "LIKE"},  # not an operator we allow
        {**QTY, "sql": "DROP TABLE x"},  # unknown fields are refused, never passed on
        {**QTY, "kind": "raw_sql"},
        {**TOTAL, "tolerance": -1},
    ],
)
def test_anything_outside_the_format_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_spec(bad)


def test_validation_against_the_catalog() -> None:
    catalog = _catalog()
    assert validate(parse_spec(SHIPPED), catalog, LINKS) == []
    assert validate(parse_spec(TOTAL), catalog, LINKS) == []
    assert validate(parse_spec(QTY), catalog, LINKS) == []

    assert validate(parse_spec({**QTY, "column": "nope"}), catalog) == [
        "unknown column shop.orders.nope"
    ]
    assert validate(parse_spec({**QTY, "column": "email"}), catalog) == [
        "shop.orders.email is not numeric"
    ]
    same_row = {"kind": "compare_columns", "table": "shop.shipments", "column": "shipped_at",
                "op": ">=", "other_column": "carrier"}
    assert "can't be compared" in validate(parse_spec(same_row), catalog)[0]
    # joins must follow a known relationship
    assert validate(parse_spec(SHIPPED), catalog, links=[])[0].startswith("no known relationship")
    assert validate(parse_spec(SHIPPED), catalog) == []  # approved rules aren't re-judged


def test_compiled_sql_quotes_names_and_binds_values() -> None:
    q = compile_rule(parse_spec(TOTAL), '"legacy"."INV_HDR" AS t')
    assert "CROSS JOIN LATERAL" in q.source
    assert 'c."INV_NO" = t."INV_NO"' in q.source
    assert ":tolerance" in q.broken
    assert q.params == {"tolerance": 0.01}
    constant = compile_rule(parse_spec(QTY), '"shop"."orders" AS t')
    assert constant.params == {"value": 0.0}
    assert ":value" in constant.broken  # the value is bound, not inlined


# --- what the LLM sees ---------------------------------------------------------------------


def test_context_shares_statistics_but_no_text_values() -> None:
    context = schema_context(_catalog(), [(LINKS[0], "proposed", 0.93)], RuleSettings())
    orders = next(t for t in context["tables"] if t["table"] == "shop.orders")
    columns = {c["name"]: c for c in orders["columns"]}
    assert columns["qty"]["min"] == "1"
    assert columns["qty"]["null_rate"] == 0.1
    assert "min" not in columns["email"]
    assert "x.io" not in json.dumps(context)
    assert context["relationships"] == [
        {"child": "shop.shipments(ord_id)", "parent": "shop.orders(order_id)",
         "status": "proposed", "confidence": 0.93}
    ]  # fmt: skip
    hidden = schema_context(_catalog(), [], RuleSettings(share_numeric_ranges=False))
    assert '"min"' not in json.dumps(hidden)


# --- reading the LLM's answer --------------------------------------------------------------


def test_rules_reply_parsing() -> None:
    assert parse_rules_reply(json.dumps({"rules": [QTY]})) == [QTY]
    assert parse_rules_reply(json.dumps([QTY, "junk"])) == [QTY]
    assert parse_rules_reply("```json\n" + json.dumps({"rules": [QTY]}) + "\n```") == [QTY]
    with pytest.raises(LLMError):
        parse_rules_reply("Sure! Here are some rules:")


# --- providers -----------------------------------------------------------------------------


def _settings(**kw: Any) -> Settings:
    # model_validate skips the environment and .env, so a real key never leaks in.
    return Settings.model_validate({"database_url": "postgresql://x", **kw})


def test_provider_switch() -> None:
    with pytest.raises(LLMNotConfiguredError):
        llm_from_settings(_settings(llm_provider="none"))
    with pytest.raises(LLMNotConfiguredError):
        llm_from_settings(_settings(llm_provider="gemini", gemini_api_key=None))
    client = llm_from_settings(_settings(llm_provider="gemini", gemini_api_key="k", llm_model=""))
    assert (client.provider, client.model) == ("gemini", llm_module.GEMINI_DEFAULT_MODEL)


class _Response(io.BytesIO):
    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def test_gemini_request_keeps_the_key_out_of_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _Response:
        sent["url"], sent["headers"] = request.full_url, dict(request.header_items())
        sent["body"] = json.loads(request.data)
        reply = {
            "candidates": [{"content": {"parts": [{"text": '{"rules": []}'}]}}],
            "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 8},
            "modelVersion": "gemini-test",
        }
        return _Response(json.dumps(reply).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)  # what llm.py calls
    reply = GeminiClient("secret-key", "gemini-test").complete_json("system", "prompt")

    assert "secret-key" not in sent["url"]
    assert sent["url"].startswith("https://")
    assert sent["headers"]["X-goog-api-key"] == "secret-key"
    assert sent["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert (reply.text, reply.model, reply.input_tokens) == ('{"rules": []}', "gemini-test", 120)


def test_gemini_reply_without_an_answer() -> None:
    with pytest.raises(LLMError, match="SAFETY"):
        parse_gemini_reply({"promptFeedback": {"blockReason": "SAFETY"}}, "m")
    with pytest.raises(LLMError, match="MAX_TOKENS"):
        parse_gemini_reply({"candidates": [{"finishReason": "MAX_TOKENS"}]}, "m")


# --- lab scoring of proposals --------------------------------------------------------------


def test_proposal_coverage_of_hidden_rules() -> None:
    same_row_flipped = parse_spec(
        {"kind": "compare_columns", "table": "shop.shipments", "column": "shipped_at",
         "op": "<=", "other_column": "delivered_at"}
    )  # fmt: skip
    found = [
        found_rule(parse_spec(SHIPPED), RuleStatus.ACTIVE, "a"),
        found_rule(same_row_flipped, RuleStatus.PROPOSED, "b"),
        found_rule(parse_spec(QTY), RuleStatus.REJECTED, "c"),  # not in the answer key
    ]
    report = score_rules(HIDDEN_RULES, found)
    assert {(r.table, r.column) for r in report.found} == {
        ("shop.shipments", "shipped_at"),
        ("shop.shipments", "delivered_at"),
    }
    assert [r.text for r in report.unmatched] == ["c"]
    assert (report.approved, report.rejected) == (1, 1)
