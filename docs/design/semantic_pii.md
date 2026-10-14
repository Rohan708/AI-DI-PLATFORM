# Design: personal data, semantic checks (Stage 2.6, anomaly category 8)

**Status:** code written 2026-10-11.
**Code:** `semantic/` (patterns, judging, runner, `lumida pii`), `ingestion/sql/pii.py` (the in-database count, used by both adapters). It runs as the `semantic` step of `lumida run`, or with `lumida pii NAME`.

## 1. What it answers
1. **Where does personal data live?** A map of columns that hold email addresses, phone numbers, card numbers, IBANs, IP addresses, US SSNs, Indian PAN and Aadhaar numbers. That's what GDPR, DPDP and SOC 2 questionnaires ask for, and few teams can answer it for a legacy database.
2. **Where is it that shouldn't be?** Personal data in a column whose name doesn't announce it: emails typed into a name field, phone numbers in free-text notes, card numbers in a comment. Copies like these leak through exports, logs and analytics, and are missed by access rules and deletion requests. **These are the findings.**
3. **Same concept, different names:** the same kind of data held under several column names (`email`, `E_MAIL`, `contact_addr`). Shown in the map.

## 2. Privacy: counted inside the database
For each table, one query runs on at most 10,000 rows, inside the customer's database. It counts non-NULL values and matches per text column and pattern.
- **Patterns are bind parameters**, never pasted into SQL.
- **Only counts come back.** No value is read out, logged or stored.
- **Findings hold counts and shares only.** A test checks that no `@` appears in the evidence.

| Database | Regular expressions |
|---|---|
| Postgres | `~` |
| MySQL 8 / Oracle | `REGEXP_LIKE(col, pattern, 'c')` |
| Snowflake | `REGEXP_LIKE(col, '.*(pattern).*', 'cs')` (matches the whole value there) |
| SQL Server | none: the check reports "skipped" (planned: `LIKE`-based email/phone) |

The patterns use the regular-expression subset all of these, and Python, read the same way. They have boundaries, so ShopCo's tracking numbers, customer codes and SKUs don't match (unit-tested).

## 3. The judgement (`semantic/classify.py`, settings in `SemanticSettings`)
| | Rule | Default |
|---|---|---|
| **holds** (PII map) | share of non-NULL values that match | ≥ 80% |
| **unexpected** (finding) | the column's name doesn't announce the kind (after abbreviation expansion: `CUST_NM` → *customer name*), and at least N matching values and share | ≥ 5 values and ≥ 0.5% |
| severity | medium; **high** for card numbers, IBANs, SSNs, PAN, Aadhaar | |

**What counts as a name announcing a kind:** each kind lists its name words (`email`, `mail`, `contact` · `phone`, `mobile`, `tel` · `card`, `cc` · `iban`, `bank` · `ip`, `host` · `ssn` · `pan`, `tax` · `aadhaar`). So `CONTACT_EMAIL` holding emails is expected, while `CUST_NM` holding emails is not.

**Finding lifecycle:** each finding is a condition per (column, kind), and resolves when the values are gone. The map is stored on each column's latest profile (`column_profile.extra.pii`) for docs and the future UI.

## 4. Lab
New scenario **`emails_in_names`** (Stage 2): 3% of legacy customers, at least 8, get their email address in `legacy.CUST_MASTER.CUST_NM`. The integration test checks three things:
- the clean lab has personal data only where the names say so (0 findings), and `shop.customers.email` is mapped
- the planted emails are found with the exact count
- the finding resolves after cleanup

## 5. Known limitations
- Patterns are lexical. A 16-digit number that isn't a card still matches; Luhn check-digit validation would need values out of the database (or a per-database UDF).
- Names, addresses and dates of birth aren't detected from values; they're hard to tell apart from other text without AI and sample values (backlog: opt-in AI classification).
- SQL Server isn't supported yet, and views aren't scanned.
- It samples the first 10,000 rows of each table, not a random sample.
