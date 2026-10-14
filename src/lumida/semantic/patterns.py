"""What personal data looks like, and which column names announce it.

The patterns run *inside* the customer's database (as bind parameters, never pasted into
SQL); only counts come back. They are written in the regular-expression subset that
Postgres, MySQL 8, Oracle, Snowflake and Python all read the same way: character classes,
``{m,n}``, ``\\.``, ``\\(`` (no ``\\d``, no look-arounds). Each pattern finds the kind
anywhere inside a value ("call 555-014-2671 after 6" counts as a phone number), with
non-digit/non-letter boundaries so long codes don't match by accident.
"""

from dataclasses import dataclass

from lumida.discovery.naming import tokens


@dataclass(frozen=True)
class PiiKind:
    name: str
    label: str  # for people: "email addresses"
    pattern: str
    name_words: frozenset[str]  # column-name words that say "this column holds it"
    sensitive: bool = False  # card numbers, national ids: higher severity


PII_KINDS: tuple[PiiKind, ...] = (
    PiiKind(
        "email", "email addresses",
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}",
        frozenset({"email", "mail", "e", "contact", "login", "username", "user"}),
    ),
    PiiKind(
        "phone", "phone numbers",
        r"(^|[^0-9])(\+[0-9]{1,3}[ .-]?)?\(?[0-9]{3}\)?[ .-]?[0-9]{3}[ .-][0-9]{4}([^0-9]|$)",
        frozenset({"phone", "tel", "telephone", "mobile", "cell", "fax", "contact", "number"}),
    ),
    PiiKind(
        "card", "payment card numbers",
        r"(^|[^0-9])[0-9]{4}[ -]?[0-9]{4}[ -]?[0-9]{4}[ -]?[0-9]{1,7}([^0-9]|$)",
        frozenset({"card", "cc", "pan", "credit"}),
        sensitive=True,
    ),
    PiiKind(
        "iban", "bank account numbers (IBAN)",
        r"(^|[^A-Za-z0-9])[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}([^A-Za-z0-9]|$)",
        frozenset({"iban", "bank", "account"}),
        sensitive=True,
    ),
    PiiKind(
        "ip_address", "IP addresses",
        r"(^|[^0-9.])([0-9]{1,3}\.){3}[0-9]{1,3}([^0-9.]|$)",
        frozenset({"ip", "host", "remote", "client"}),
    ),
    PiiKind(
        "us_ssn", "US social security numbers",
        r"(^|[^0-9])[0-9]{3}-[0-9]{2}-[0-9]{4}([^0-9]|$)",
        frozenset({"ssn", "social", "security"}),
        sensitive=True,
    ),
    PiiKind(
        "in_pan", "Indian PAN numbers",
        r"(^|[^A-Za-z0-9])[A-Z]{5}[0-9]{4}[A-Z]([^A-Za-z0-9]|$)",
        frozenset({"pan", "tax", "tin"}),
        sensitive=True,
    ),
    PiiKind(
        "in_aadhaar", "Aadhaar numbers",
        r"(^|[^0-9])[2-9][0-9]{3}[ -][0-9]{4}[ -][0-9]{4}([^0-9]|$)",
        frozenset({"aadhaar", "aadhar", "uid", "uidai"}),
        sensitive=True,
    ),
)  # fmt: skip

KINDS_BY_NAME = {k.name: k for k in PII_KINDS}


def expected_kinds(column_name: str) -> set[str]:
    """Kinds the column's name announces (``CONTACT_EMAIL`` -> email; ``mobile_no`` ->
    phone). Abbreviations are expanded the same way discovery does it."""
    words = set(tokens(column_name)) | {column_name.lower()}
    return {k.name for k in PII_KINDS if words & k.name_words}
