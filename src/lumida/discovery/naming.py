"""Turn messy identifiers into comparable words, and score how well a column's name
suggests it points at a table.

    cust_no      -> ["customer", "number"]
    CUSTID       -> ["customer", "id"]
    orderRef     -> ["order", "reference"]
    INV_LINE_TAX -> ["invoice", "line", "tax"]
    addresses    -> ["address"]
"""

import re

# Common abbreviations in legacy and hand-written schemas (all lowercase).
ABBREVIATIONS: dict[str, str] = {
    "acct": "account",
    "addr": "address",
    "adr": "address",
    "amt": "amount",
    "cat": "category",
    "cd": "code",
    "cust": "customer",
    "cus": "customer",
    "dept": "department",
    "dt": "date",
    "emp": "employee",
    "ext": "external",
    "hdr": "header",
    "inv": "invoice",
    "itm": "item",
    "nbr": "number",
    "nm": "name",
    "no": "number",
    "nr": "number",
    "num": "number",
    "ord": "order",
    "pmt": "payment",
    "prd": "product",
    "prod": "product",
    "qty": "quantity",
    "ref": "reference",
    "ship": "shipment",
    "stat": "status",
    "trx": "transaction",
    "txn": "transaction",
    "usr": "user",
}
# Words that mark a column as an identifier/key.
KEY_WORDS = frozenset({"id", "number", "code", "key", "reference"})
# Suffixes glued onto a word without a separator, e.g. CUSTID, INVNO.
_GLUED_SUFFIXES = ("id", "no", "cd", "num", "nbr", "ref", "key", "code")
# Words in table names that don't name the entity (CUST_MASTER is about customers).
NON_ENTITY_WORDS = frozenset(
    {"master", "header", "table", "tbl", "dim", "fact", "data", "info", "main", "base"}
)
# Words that make a column a measure, not a reference ("orders_count", "total_amount").
MEASURE_WORDS = frozenset(
    {"count", "total", "sum", "amount", "quantity", "avg", "average", "min", "max",
     "lifetime", "revenue", "price", "balance", "size", "length", "rate", "pct", "percent"}
)  # fmt: skip
# Generic surrogate-key names: a column named just "id" is its own table's key.
GENERIC_KEY_NAMES = frozenset({"id", "pk", "key"})

# Name-score levels (see docs/design/relationship_discovery.md).
SCORE_EXACT_COLUMN = 1.0  # INV_HDR.CUSTID -> CUST_MASTER.CUSTID
SCORE_ENTITY_AND_KEY = 0.8  # cust_no -> customers
SCORE_ENTITY_ONLY = 0.6  # ship_addr -> addresses
BONUS_SINGLE_ENTITY_TABLE = 0.05  # customers beats customer_summary for cust_no

_SPLIT = re.compile(r"[^0-9A-Za-z]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokens(identifier: str) -> list[str]:
    words: list[str] = []
    for part in _SPLIT.split(identifier):
        for word in _CAMEL.split(part):
            if word:
                words.extend(_unglue(word.lower()))
    return [_singular(ABBREVIATIONS.get(w, w)) for w in words]


def entity_words(table_name: str) -> list[str]:
    """The words of a table name that name its entity."""
    return [w for w in tokens(table_name) if w not in NON_ENTITY_WORDS]


def name_score(child_column: str, parent_table: str, parent_column: str) -> float:
    """How strongly the child column's name suggests it references parent_table.parent_column."""
    if child_column == parent_column and child_column.lower() not in GENERIC_KEY_NAMES:
        return SCORE_EXACT_COLUMN
    child = set(tokens(child_column))
    entity = entity_words(parent_table)
    if not entity or entity[0] not in child:
        return 0.0
    if child & MEASURE_WORDS or _counts_entity(child_column, entity[0], child):
        return 0.0
    score = SCORE_ENTITY_AND_KEY if child & KEY_WORDS else SCORE_ENTITY_ONLY
    if len(entity) == 1:
        score += BONUS_SINGLE_ENTITY_TABLE
    return score


def _counts_entity(child_column: str, entity: str, child_tokens: set[str]) -> bool:
    """``lifetime_orders`` / ``orders`` (plural, no key word) is a count of orders."""
    raw = {w.lower() for part in _SPLIT.split(child_column) for w in _CAMEL.split(part) if w}
    plural = {entity + "s", entity + "es", entity[:-1] + "ies" if entity.endswith("y") else ""}
    return bool(raw & plural) and not child_tokens & KEY_WORDS


def _unglue(word: str) -> list[str]:
    for suffix in _GLUED_SUFFIXES:
        if word.endswith(suffix) and len(word) > len(suffix) + 1 and word not in ABBREVIATIONS:
            stem = word[: -len(suffix)]
            if stem in ABBREVIATIONS or len(stem) >= 3:
                return [stem, suffix]
    return [word]


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "uses")) or (word.endswith("ses") and len(word) > 4):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")) and len(word) > 3:
        return word[:-1]
    return word
