# Relationship discovery, relationship checks & auto-documentation (Stage 1.4)

*Code: `src/ai_data_engineer/discovery/` (+ `ingestion/postgres/relationships.py`, `detection/recording.py`). Commands: `aide discover`, `aide relationships`, `aide docs`.*

Most legacy databases declare few or none of their foreign keys, and name them inconsistently (`cust_no`, `CUSTID`, `client_ref`). This stage finds the real links from **evidence**, explains each one, and uses them for the first findings (orphan rows) and for generated documentation. It is fully deterministic: no AI calls. AI review of ambiguous cases comes in Stage 2.

```
aide discover shopco
  1. load what the latest scan knows (tables, columns, measurements, keys)
  2. declared FKs                         -> relationships (certain)
  3. read the query log                   -> joins the application really runs
  4. generate candidates, rank them       -> check the best ones in the database (value overlap)
  5. score -> keep >= 0.6 -> one explanation per child column(s)
  6. save as "proposed" (never overriding a human decision)
  7. relationship checks                  -> findings (orphan rows, non-unique parent keys)
```

---

## 1. Candidates

**Parents are keys:**
- primary keys
- unique constraints
- **historical keys**: a primary key the table had in an earlier version. A legacy table that lost its PK still has one logically; the lab's `drop_primary_key` scenario does exactly this.
- **measured-unique columns**: the latest scan shows every non-null value distinct, e.g. `customers.customer_code` or `CUST_MASTER.EXT_REF`.

**Children** are columns of the same type family (integer, string or uuid) that pass cheap pruning using the measurements we already have, with no database query:
- Even at the minimum overlap, the child's distinct values must fit in the parent's: `distinct_child × 0.8 ≤ distinct_parent`. This rule survives orphans, because orphans add values.
- Numeric ranges must overlap.
- Text lengths must be within ±50% of each other.
- A column named just `id`/`pk`/`key` is its own table's key and is never a child.

Text min/max are deliberately **not** compared: the database sorts text by its collation, which Python's ordering doesn't reproduce.

**Composite keys** (e.g. `INV_LINE_TAX(INV_NO, LINE_NO) → INV_LINE`) are matched only against existing composite keys with **identical column names**. Trying every column combination would explode on large schemas.

## 2. Evidence

### Names (`naming.py`)
Identifiers are split and normalised:
- `cust_no` → *customer number*
- `CUSTID` → *customer id*
- `orderRef` → *order reference*
- `INV_LINE_TAX` → *invoice line tax*
- `addresses` → *address*

Abbreviations are expanded from a dictionary (`cust`, `ord`, `addr`, `inv`, `prod`, `ref`, `no`, `cd`, …) and plurals are singularised. Then:

| Situation | Name score | Example |
|---|---|---|
| identical column name (not a generic `id`) | 1.0 | `INV_HDR.CUSTID → CUST_MASTER.CUSTID` |
| names the parent's entity + a key word (`id`, `number`, `code`, `key`, `reference`) | 0.8 (+0.05 if the parent table name is a single entity) | `cust_no → customers` = 0.85 |
| names the entity, no key word | 0.6 (+0.05) | `ship_addr → addresses` = 0.65 |
| a **measure or count** (`count`, `total`, `amount`, `lifetime`, …, or the plural entity without a key word) | 0 | `orders_count`, `lifetime_orders` are counts, not links |
| nothing in common | 0 | `EXT_REF → customers` |

The entity of a table is its first meaningful word, so `CUST_MASTER` → *customer* and `INV_HDR` → *invoice*.

### Value overlap (in the customer's database, read-only)
This is the share of child **rows** (non-null key) whose value exists in the parent, measured on up to 10,000 rows (sampled on large tables). It's per row rather than per distinct value, so 2% orphan rows show up as 98% overlap, which is the true orphan rate.

### Query log (`querylog.py`)
`pg_stat_statements` keeps the application's normalised statements and their call counts. Each statement is parsed with **sqlglot**, and equality conditions between columns of two different tables are collected, with aliases and schemas resolved. `JOIN customers c ON o.cust_no = c.id` becomes `shop.orders.cust_no = shop.customers.id`, with its calls. Unqualified columns are ignored; we don't guess. If the extension isn't available, discovery works without this signal.

### Gates
- **Integer keys need a name or query-log signal.** Small 1…N numbers "fit inside" every other ID column: `order_items.quantity` (values 1–3) is 100% contained in `categories.id`. Without a name match or a logged join, an integer candidate isn't even checked.
- **Text can match on values alone if the values are distinctive:** the parent's average length is ≥ 4 and the child has ≥ 5 distinct values. Codes like `CU00000017` or `SKU-00042` don't overlap by accident. UUIDs are always distinctive.

## 3. Confidence

```
confidence = 0.50 × inclusion_term        (0 at 80% overlap, 1 at 100%)
           + 0.25 × name_score
           + 0.25 × [the application joins these columns]
           + 0.20 × [values are distinctive codes]
capped at 0.99; candidates below 80% overlap are rejected; >= 0.60 → stored as "proposed"
```

Worked examples on ShopCo:

| Relationship | Overlap | Name | Query log | Distinctive | Confidence |
|---|---|---|---|---|---|
| `orders.cust_no → customers.id` | 100% | 0.85 | yes | – | **0.96** |
| same, after 2% orphans planted | 98% | 0.85 | yes | – | **0.91** |
| `orders.ship_addr → addresses.id` | 100% | 0.65 | – | – | **0.66** |
| `CUST_MASTER.EXT_REF → customers.customer_code` | 100% | 0 | – | yes | **0.70** |
| `INV_HDR.CUSTID → CUST_MASTER.CUSTID` | 100% | 1.0 | – | yes | **0.95** |
| `customers.customer_code → CUST_MASTER.EXT_REF` (reverse) | ~85% | 0 | – | yes | 0.33, rejected |

Every relationship stores its **signals** (`value_inclusion`, `name_match`, `query_log_join`, `distinctive_values`, or `declared_fk`) and its **evidence** (overlap, rows checked, name score, call count, which key was used, and human-readable reasons). All weights are named settings in `discovery/settings.py`.

### One explanation per child
- A child column (or column set) points to **one** parent: the most confident. `orders.cust_no` matches both `customers.id` (0.96) and `customer_summary.customer_id` (~0.7), and only the first is kept.
- A 1:1 pair found in both directions keeps the more confident direction.
- A single-column link already covered by a composite link from the same child columns is dropped. `INV_LINE_TAX.INV_NO → INV_HDR` is implied by `INV_LINE_TAX(INV_NO, LINE_NO) → INV_LINE`.

## 4. Storing relationships and human review
| Situation | What happens |
|---|---|
| declared FK | `declared` / `confirmed`, no confidence |
| new inferred link | `inferred` / `proposed`, with confidence and evidence |
| found again | evidence refreshed; confidence refreshed unless a person reviewed it |
| a person confirmed/rejected it | **status kept forever**; rejected links are never re-proposed |
| no longer supported | closed (`valid_to`), unless a person reviewed it; history stays queryable |

```
aide relationships list shopco [--all]
aide relationships confirm 3fa2c1d0 [--by alice]
aide relationships reject  91b0e7aa
```

## 5. Relationship checks: the first findings
| Check | Runs on | Finding |
|---|---|---|
| `orphan_rows` | confirmed relationships, and proposed ones with confidence ≥ **0.9**; declared FKs are skipped (the database enforces them) | "148 rows in shop.orders point to missing shop.customers rows". Evidence: counts, fraction, **identifiers of up to 20 offending rows** (the child's PK), and whether the relationship was confirmed or only proposed. Severity: high at ≥ 5% orphans, else medium. |
| `parent_key_not_unique` | any relationship whose parent is a single column | "…is referenced as a key but has duplicates" (joins fan out). Uses the latest measurements, no extra query. |

Findings are written through `detection/recording.py`:
- The same problem seen again **refreshes** the open finding (one finding, `last_detected_at` moves); there are no duplicates.
- When a later run sees the problem is gone, the finding is **resolved**.
- The full lifecycle (alerts, reopening) comes in Stage 1.6.

## 6. Auto-documentation
`aide docs shopco [--out DIR]` writes `generated_docs/shopco/` (git-ignored):

| File | Contents |
|---|---|
| `README.md` | overview, counts, and a **Mermaid relationship map**: solid lines for declared FKs, dotted for inferred ones labelled with confidence |
| `data_dictionary.md` | every table: rows, primary key, what it references, what references it, and each column with type, null %, distinct count and top values |
| `relationships.md` | every relationship with kind, status, confidence, signals and the reasons it was proposed |

This is what lets someone who just inherited an undocumented database understand it in minutes, and it's a strong demo.

## 7. Settings (`DiscoverySettings`)
| Setting | Default |
|---|---|
| `min_inclusion` | 0.8 |
| `min_name_score_for_integers` | 0.6 |
| `min_distinctive_length` / `min_child_distinct` | 4 / 5 |
| `length_tolerance` | 0.5 |
| weights: inclusion / name / query log / distinctive bonus | 0.5 / 0.25 / 0.25 / 0.2 |
| `propose_threshold` | 0.6 |
| `orphan_check_min_confidence` | 0.9 |
| `orphan_high_severity_fraction` | 0.05 |
| `orphan_sample_size` | 20 |
| `max_values_checked` | 10,000 rows per overlap query |
| `max_inclusion_checks` | 200 overlap queries per run |
| `query_log_limit` | 1,000 statements |

## 8. Done when (benchmark)
On the planted lab, after nightly scans and one discovery run:
- at least **13 of 15** true relationships found
- at most **2 wrong**
- the planted **`orphan_orders`** caught

Integration tests assert exactly this.

## 9. Results log
| Date | Run | Relationships found | Wrong | Notes |
|---|---|---|---|---|
| 2026-10-05 | lab `standard`, size `small`, seed 42, 15 nightly scans | **15 / 15** | 1 | `orphan_orders` caught, 0 false alarms. Wrong one: `shipments.shipment_id → shipments.ord_id` (0.63): a table's own PK matched another unique column of the same table. **Fixed:** a table's own primary key is never treated as a reference to the same table. |

| 2026-10-07 | same lab, re-discovery after a laptop restart | **15 / 15** | **0** | Wrong link retired automatically. Postgres restart wiped `pg_stat_statements` (167 → 12 statements), so query-log-backed confidences fell (e.g. `cust_no` 0.91 → 0.66) and the orphan check didn't re-run. Fix planned: persist join evidence (Stage 1.5). |

## 10. Known limitations
- Composite links are only found when column names match exactly.
- Relationships across *different* data sources (source DB ↔ warehouse) come with cross-system checks in Stage 3.
- Settings are global defaults for now; per-source overrides come later.
- Query-log evidence needs `pg_stat_statements`, and is **lost when Postgres restarts** until we persist it (Stage 1.5). Without it, integer relationships rely on names alone.
