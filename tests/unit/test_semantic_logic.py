"""Personal-data patterns and judgements, without a database. The patterns are checked
with Python's ``re``, which reads the same subset the databases do."""

import re

import pytest

from lumida.ingestion.base import PiiCounts
from lumida.semantic.classify import (
    classify,
    holds,
    same_kind_different_names,
    unexpected,
)
from lumida.semantic.patterns import KINDS_BY_NAME, expected_kinds
from lumida.semantic.settings import SemanticSettings


def _hit(kind: str, value: str) -> bool:
    return re.search(KINDS_BY_NAME[kind].pattern, value) is not None


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("email", "anna.berg.17@gmail.com"),
        ("email", "mail me: ops@shop.co.uk please"),
        ("phone", "(555)123-4567"),
        ("phone", "+1-555-123-4567x890"),
        ("phone", "call 555.014.2671 after 6"),
        ("card", "4111 1111 1111 1111"),
        ("card", "4111111111111111"),
        ("iban", "DE89370400440532013000"),
        ("ip_address", "login from 192.168.10.4"),
        ("us_ssn", "078-05-1120"),
        ("in_pan", "ABCDE1234F"),
        ("in_aadhaar", "2345 6789 0123"),
    ],
)
def test_patterns_find_personal_data(kind: str, value: str) -> None:
    assert _hit(kind, value)


@pytest.mark.parametrize(
    "value",
    [
        "TRK0000012345",  # ShopCo tracking numbers
        "CU00000017",  # customer codes
        "ELE-0001",  # SKUs
        "123 Main Street Suite 456",
        "JOHN SMITH",
        "2026-03-15 09:00:00+00",
        "v1.2.3",  # a version, not an IP address
    ],
)
def test_ordinary_values_are_not_personal_data(value: str) -> None:
    assert [k for k in KINDS_BY_NAME if _hit(k, value)] == []


def test_column_names_announce_their_kind() -> None:
    assert expected_kinds("email") == {"email"}
    assert expected_kinds("CONTACT_EMAIL") >= {"email"}
    assert "phone" in expected_kinds("mobile_no")
    assert expected_kinds("CUST_NM") == set()


def test_judging_counts() -> None:
    counts = PiiCounts(
        rows_checked=1000,
        non_null={"email": 990, "CUST_NM": 1000, "notes": 400},
        matches={
            "email": {"email": 985, "phone": 0},
            "CUST_NM": {"email": 21, "phone": 0},  # emails typed into the name field
            "notes": {"email": 1, "phone": 3},  # too few to matter
        },
    )
    s = SemanticSettings()
    found = classify("legacy.CUST_MASTER", counts)

    assert [(f.column, f.kind) for f in holds(found, s)] == [("email", "email")]
    stray = unexpected(found, s)
    assert [(f.column, f.kind, f.matching) for f in stray] == [("CUST_NM", "email", 21)]
    assert round(stray[0].share, 3) == 0.021


def test_same_kind_under_different_names() -> None:
    a = classify("shop.customers", PiiCounts(10, {"email": 10}, {"email": {"email": 10}}))
    b = classify("crm.contacts", PiiCounts(10, {"E_MAIL": 10}, {"E_MAIL": {"email": 10}}))
    groups = same_kind_different_names(holds(a + b, SemanticSettings()))
    assert groups == {"email": ["crm.contacts.E_MAIL", "shop.customers.email"]}
