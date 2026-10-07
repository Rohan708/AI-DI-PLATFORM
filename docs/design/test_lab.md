# The messy test lab & benchmark (Stage 1.2)

*Code: `src/ai_data_engineer/lab/`. Commands: `aide lab …`. Answer keys: `validation/answer_keys/`. Reports: `validation/reports/`.*

## Why it exists
We can't build an anomaly detector against clean data, and we can't wait for customers to tell us whether it works. The lab is a **fake company database that is messy on purpose**, with a **written answer key** of every planted problem. Every later stage is scored against it:

| Stage | What the lab proves |
|---|---|
| 1.3 adapter | the metadata store matches the lab database |
| 1.4 relationship discovery | how many of the **11 hidden relationships** we find, and how many wrong ones we propose |
| 1.5 detection | how many **planted anomalies** we catch, and how many **false alarms** we raise |
| 2 AI rules | whether approved AI rules catch the planted **business-rule** violations |

It also doubles as a **demo database** for sales ("try it on our sample company"), and the benchmark number is a sales line ("caught 47 of 50 planted problems, 2 false alarms").

The lab only ever writes to **its own database** (a separate Postgres on port 5433). It refuses to build anywhere that already contains non-lab content.

---

## The company: "ShopCo"
A retailer with a modern web shop, an old billing system that was never retired, and a nightly reporting job.

| Schema | Tables | Built-in mess |
|---|---|---|
| `shop` (the app) | `categories`, `products`, `customers`, `addresses`, `orders`, `order_items`, `payments`, `shipments` | only 4 FKs declared; the rest are hidden and **misnamed** (`orders.cust_no`, `payments.order_ref`, `shipments.ord_id`) |
| `legacy` (old billing) | `CUST_MASTER`, `INV_HDR`, `INV_LINE`, `INV_LINE_TAX` | UPPERCASE names, `CHAR` types, **no FKs at all**, a **composite key** (`INV_NO`, `LINE_NO`), links to the app by **business codes** (`EXT_REF` = customer code, `ITEM_CD` = SKU) |
| `reporting` (nightly ETL) | `daily_sales`, `customer_summary` | loaded once a night; this is what freshness and volume checks watch |
| `aide_lab` | `state` | the lab's own bookkeeping; **scanners must exclude it** |

### The 15 true relationships (4 declared, 11 hidden)
| From | To | Declared? |
|---|---|---|
| `shop.categories(parent_id)` | `shop.categories(id)` | yes |
| `shop.products(category_id)` | `shop.categories(id)` | yes |
| `shop.addresses(customer_id)` | `shop.customers(id)` | yes |
| `shop.order_items(order_id)` | `shop.orders(order_id)` | yes |
| `shop.orders(cust_no)` | `shop.customers(id)` | **no**, misnamed |
| `shop.orders(ship_addr)` | `shop.addresses(id)` | **no**, misnamed |
| `shop.order_items(product_id)` | `shop.products(id)` | **no** |
| `shop.payments(order_ref)` | `shop.orders(order_id)` | **no** |
| `shop.shipments(ord_id)` | `shop.orders(order_id)` | **no** |
| `legacy.CUST_MASTER(EXT_REF)` | `shop.customers(customer_code)` | **no**, cross-schema by business code |
| `legacy.INV_HDR(CUSTID)` | `legacy.CUST_MASTER(CUSTID)` | **no** |
| `legacy.INV_LINE(INV_NO)` | `legacy.INV_HDR(INV_NO)` | **no** |
| `legacy.INV_LINE(ITEM_CD)` | `shop.products(sku)` | **no**, cross-schema by business code |
| `legacy.INV_LINE_TAX(INV_NO, LINE_NO)` | `legacy.INV_LINE(INV_NO, LINE_NO)` | **no**, composite |
| `reporting.customer_summary(customer_id)` | `shop.customers(id)` | **no** |

Only **5** of the 11 hidden relationships show up in the application's query log (see "a simulated day" below). The other 6 must be found from names, types and value overlap, as in real life.

### Normal patterns: traps for false alarms
These look odd but are **not** problems. A good detector stays quiet about them:
- `customers.middle_name` is ~60% NULL, and `customers.phone` is ~40% NULL
- `products.weight_grams` is NULL for every digital product
- `shipments.delivered_at` is NULL while a parcel is in transit
- `CUST_MASTER.EXT_REF` is NULL for legacy-only customers (~5%)
- legacy `CHAR` columns are space-padded
- weekend order volume is ~40% lower (seasonality)
- the order status mix shifts daily as orders move placed → shipped → delivered

---

## How the simulation works
**Everything is seeded.** The same seed and size always produce identical data. Each simulated day has its own random generator derived from `seed + date`, so day 37 is the same whether it was reached by `build` or by `tick`.

`build` creates the catalog and customers, then replays `history_days` days of activity through the **same** day simulation that `tick` uses.

### A simulated day
1. **Sign-ups:** about 1% new customers. 85% are mirrored into `legacy.CUST_MASTER`.
2. **Orders:** a baseline × weekday factor (Monday 1.0 … Friday 1.25, Saturday 0.65, Sunday 0.55) × ±10% noise. Each order has 1–4 items and a payment; 3% are cancelled.
3. **Fulfilment:** orders 2 days old ship; parcels shipped 3 days ago are delivered.
4. **Legacy billing batch:** today's orders from mirrored customers become invoices, lines and tax lines. Invoices are marked paid after 14 days.
5. **Nightly ETL:** appends the day to `reporting.daily_sales` and fully refreshes `reporting.customer_summary` (stamped 02:00 the next morning), unless a fault skips it.
6. **Application queries:** the "app" runs its usual joins, so `pg_stat_statements` collects real query-log evidence for relationship discovery.

### Sizes
| Size | Customers | Products | History days | Orders/day (baseline) | Use |
|---|---|---|---|---|---|
| `tiny` | 60 | 20 | 20 | 8 | automated tests |
| `small` | 600 | 80 | 60 | 40 | quick manual runs |
| `default` | 3,000 | 200 | 90 | 150 | the benchmark |

---

## Planted anomalies (scenarios)
Each scenario breaks something specific **and writes its own answer-key entry** at the same time: category, table/column, the kind of check expected to catch it, the roadmap stage that should catch it, and the exact number of rows affected. Row selection is deterministic (ordered by a hash of the key and the seed).

| Scenario | Category | Where | What happens | Caught by stage |
|---|---|---|---|---|
| `drop_column` | structural | `shop.products.weight_grams` | column dropped | 1.5 |
| `widen_column_type` | structural | `shop.orders.channel` | `varchar(20)` → `varchar(50)` | 1.5 |
| `drop_primary_key` | structural | `legacy.INV_LINE` | primary key removed | 1.5 |
| `orphan_orders` | relational | `shop.orders.cust_no` | 2% of orders point to customers that don't exist (spills into `customer_summary`) | 1.4 |
| `duplicate_customers` | relational | `shop.customers.email` | 3% of customers registered twice, email differing only by case | 1.5 |
| `null_spike` | column value | `shop.addresses.postal_code` | 8% set to NULL (normally 0%) | 1.5 |
| `country_variants` | column value | `shop.addresses.country` | 20% of `US` rewritten as `USA` / `United States` | 1.5 |
| `negative_quantity` | column value | `shop.order_items.quantity` | 0.5% of lines become negative | 1.5 |
| `skipped_load` | time series | `reporting.daily_sales`, `reporting.customer_summary` | the next nightly ETL doesn't run (stale tables) | 1.5 |
| `half_load` | time series | `shop.orders` | the next day only 40% of orders arrive (spills into items, payments, legacy, reporting) | 1.5 |
| `ship_before_order` | business rule | `shop.shipments.shipped_at` | 1% shipped two days *before* the order | 2 |
| `invoice_total_mismatch` | business rule | `legacy.INV_HDR.TOTAL_AMT` | 1% of invoice totals inflated 10% over their lines | 2 |

Faults (`skipped_load`, `half_load`) are scheduled and happen on the **next** simulated day. Every entry records its `effective_day`: the first day whose scan should show the problem.

### Plans
| Plan | What it does |
|---|---|
| `standard` | build → 14 clean days (each scanned) → inject **all** scenarios → 1 more day |
| `quick` | build → 3 clean days → inject the **Stage-1** scenarios → 1 more day |

Detection needs history, so plans simulate clean days first. With `--scan SOURCE` (Stage 1.3), a **scan of the lab runs after each simulated day**, stamped 03:00 the next simulated morning (after the 02:00 ETL), just like a nightly scan of a real customer database:

```bash
aide source add shopco --connection-ref AIDE_LAB_DATABASE_URL --exclude-schemas aide_lab
aide lab run standard --size small --scan shopco
```

---

## Answer key
Written **before** detection runs, by the scenarios themselves:
- `validation/answer_keys/lab-seed<seed>-<size>-<day>.json` is for the scorer
- the matching `.md` is for humans: planted anomalies, the true relationships, and the normal patterns

## The scorer (benchmark)
`aide lab score --data-source <name>` reads our findings and relationships from the metadata store and compares them with the answer key.

**Matching rules:**
- A finding **catches** a planted anomaly if it has the same **category** and **table**, and the columns agree (either side may be table-level).
- Findings on an anomaly's **related tables** (expected knock-on effects, e.g. a half-loaded day also shrinks `order_items`) are counted as **related**, not penalised.
- Every other finding is a **false alarm**, including anything flagged on the normal patterns.
- Relationships match on child table, parent table and the set of column pairs. Column order doesn't matter, so composite keys work.

**Report** (`validation/reports/benchmark-….md` + `.json`):
- recall: anomalies caught / planted
- precision: real findings / all findings
- false alarms
- a per-category table
- relationship recall and precision
- lists of what was missed and what was wrongly flagged

Until detection exists (Stage 1.5), the score is 0 caught, which is expected.

---

## Commands
| Command | Does |
|---|---|
| `aide lab build [--seed 42] [--size default]` | (re)create the lab with history |
| `aide lab tick [--days N] [--scan SOURCE]` | simulate more days (optionally scanning after each) |
| `aide lab inject SCENARIO …` | plant anomalies (effective next day) |
| `aide lab run [standard\|quick] [--seed] [--size] [--scan SOURCE]` | full plan + answer key written (optionally scanning after each day) |
| `aide lab status` | current simulated day, pending faults, planted count |
| `aide lab scenarios` | list scenarios and plans |
| `aide lab answer-key [--out DIR]` | write the answer key |
| `aide lab score [--data-source NAME] [--out DIR]` | benchmark report |

### Full benchmark loop (from Stage 1.4)
```bash
aide source add shopco --connection-ref AIDE_LAB_DATABASE_URL --exclude-schemas aide_lab
aide lab run standard --size small --scan shopco   # build, nightly scans, plant, scan
aide discover shopco                               # relationships + orphan checks
aide lab score --data-source shopco                # caught / missed / false alarms
aide docs shopco                                   # generated data dictionary + map
```

Needs `AIDE_LAB_DATABASE_URL` (see `.env.example`) and the `lab-postgres` service from `docker-compose.yml`.
