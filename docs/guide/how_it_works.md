# How AI Data Engineer works: the whole engine in plain language

*Written for understanding, not for coding. Every number comes from real runs on the ShopCo test lab. For the "why" and the sales story see [`../product/vision_and_roadmap.md`](../product/vision_and_roadmap.md) and [`../product/sales_and_logic.md`](../product/sales_and_logic.md); for exact rules see the design docs linked in each section.*

---

## The one-minute version

Every night `aide run shopco` does these things:

```
1. SCAN      read the database's structure and take measurements          (read-only)
2. DISCOVER  figure out which tables point to which (even undeclared)
3. RULES     check the business rules a person approved                  (plain SQL)
   ROWS      find individual rows far above normal                       (in the database)
4. DETECT    compare everything with its own history and flag the unusual
5. SCORE     turn open problems into a 0–100 health score per table
6. ALERT     tell people about NEW problems, once, in one message
```

Separately, and only when asked (`aide rules propose shopco`), the **AI** reads the structure and statistics and *suggests* business rules for a person to approve (§6).

It never writes to the customer's database. Everything it learns goes into **its own** database, the *metadata store*.

---

## 1. Scan: looking without touching
*Design: [postgres_adapter.md](../design/postgres_adapter.md)*

**Structure** comes from the database's own catalog, which is the list of tables, columns, types and keys every database keeps about itself. Reading it touches no customer data.

**Measurements** come from one summary query per table, run *inside* the customer's database. Only the totals come back:

> `shop.addresses`: 1,272 rows · `postal_code` 0 NULLs · `country` 4 distinct values: US 763, CA 191, GB 190, DE 128

**Safety:**
- Every query runs in a transaction the database itself makes **read-only**, so even a bug in our code can't write.
- Each query has a 30-second limit and a 2-second lock limit.
- Big tables are **sampled**.
- The customer's DBA sees our queries labelled `aide`.

**Memory:** each table and column gets a permanent ID card (its *identity*). Structure changes are kept as *versions*, and measurements are kept as a *daily series*.

> On the night the lab dropped `products.weight_grams`, the scan recorded a new version of `products`. The history shows exactly when the column vanished.

## 2. Discover: who points to whom
*Design: [relationship_discovery.md](../design/relationship_discovery.md)*

Legacy databases rarely declare their links. ShopCo declares **4**, and the other **11** are hidden, often with names like `cust_no`, `ord_id` and `CUSTID`. For every plausible pair, discovery collects evidence:

| Evidence | ShopCo example |
|---|---|
| **The name** (abbreviations expanded) | `cust_no` → *customer number* → points at `customers` |
| **The values** (counted inside the database) | 100% of `orders.cust_no` values exist in `customers.id` |
| **The app's own queries** (query log) | the app runs `JOIN customers c ON o.cust_no = c.id` thousands of times |
| **Distinctive codes** | `CU00000017`-style codes don't match by accident; small numbers 1…N do |

These combine into a **confidence**:

> `orders.cust_no → customers.id`: values 100%, name 0.85, app joins it, so confidence **0.96**

**Guards against classic mistakes:**
- Small numbers like `quantity` (1–3) fit inside every ID column, so integers need a name or a logged join.
- Counts like `lifetime_orders` are measures, not links.
- A table's own primary key never "points" at another column of the same table.

**Result on ShopCo:** **15 of 15** relationships found, **0 wrong**.

**People decide:**
- `aide relationships confirm | reject` records the decision, and discovery never overrides it.
- Joins seen in the query log are **remembered**, so a database restart doesn't wipe the evidence.

**Two things relationships make possible:**
- the first integrity check: *"56 rows in shop.orders point to missing shop.customers rows"*, with the IDs attached
- the auto-generated **data dictionary and relationship map** (`aide docs`)

## 3. Detect: "is this normal *for this column*?"
*Design: [detection.md](../design/detection.md)*

There are no fixed thresholds about anyone's data. Each table and column is compared with **its own last 14 days**.

### How "normal" is decided
```
postal_code NULL share, last 14 nights:  0.0% 0.0% 0.0% … 0.0%
tonight:                                  8.0%
→ normal ≈ 0.0%, typical wobble tiny → tonight is far outside normal → finding
```
- **Median and "typical spread"** are used rather than averages, so one odd night in the history doesn't distort "normal".
- A change must be **statistically unusual *and* big enough to matter**, e.g. at least +2 points of NULLs.
- With fewer than **7** nights of history, a check says *"still learning"* rather than guessing.
- **Weekly rhythm:** volume is compared with **the same weekday**. A quiet Monday is compared with last Monday, not with a busy Friday.
- **Small numbers are noisy:** a table that usually gets 9 rows a night can easily get 3 (counts naturally wobble by about √9 = 3). A drop only counts when it's far bigger than that wobble, or when nothing arrives at all.
- **Batch vs trickle:** a table filled by a nightly job (its newest timestamp is always about 02:00) that gets nothing new means the job didn't run. A table that users fill all day (newest timestamp 09:00 one day, 17:00 the next) is only judged if it usually gets at least **5** new rows a night. Below that, a quiet day is just chance.

### What it catches (12 checks)
| Kind | ShopCo examples |
|---|---|
| **Structure** | *"Column shop.products.weight_grams was removed"* · *"Type of shop.orders.channel changed: varchar(20) → varchar(50)"* · *"legacy.INV_LINE lost its primary key"* · tables with no PK · relationship columns without an index |
| **Values** | *"NULLs in shop.addresses.postal_code jumped from 0.0% to 8.0%"* · *"negative values appeared in order_items.quantity (minimum −3; never below 1)"* · *"'USA' (86 rows), 'United States' (68 rows) look like variants of 'US'"* · *"32 emails differ from another only by letter case"* (duplicate customers) |
| **Time** | *"shop.orders gained 15 rows; usually about 41"* (a half-loaded day) · *"reporting.daily_sales stopped updating"* (the nightly job didn't run) |

**Result on ShopCo:**
- all **11** planted problems that Stage 1 can catch were caught, with **1** false alarm (since fixed)
- all **13** real day-one issues were found too (e.g. a table with no primary key)

## 4. Score: one number per table
*Design: [lifecycle_health_alerts.md](../design/lifecycle_health_alerts.md)*

```
table  = 100 − (critical 40, high 20, medium 10, low 3 for each open problem), never below 0
schema = average of its tables, and the worst table shown next to it
whole  = average of all tables, and the worst table
```
Showing the worst table matters. One broken critical table, like `shop.orders` at 51, can't hide behind a healthy-looking average of 87. Scores are saved every night, so you see trends (*"−6.2 since last time"*).

## 5. Alert: tell people, but only what's new
- **Night one is silent.** The first run records everything and alerts nothing (*"quiet baseline: 34 findings recorded, not alerted"*). Otherwise a messy legacy database would flood the channel on day one.
- **One message per night**, not one per problem: *"7 new problems in shopco (3 high, 4 medium)"*, then the items, worst first.
- **Each problem is alerted once.** If it comes back after being fixed, that's news, so it alerts again.
- **Medium and above** are alerted. Low ones (like a missing index) are listed, not pushed.
- **Rejecting a finding teaches the tool.** "This is normal here" means it's never raised again for that table or column.

---

## 6. Rules: what statistics can't see (Stage 2)
*Design: [ai_rules.md](../design/ai_rules.md) · setup: [llm_setup.md](../setup/llm_setup.md)*

Some problems look normal column by column. A parcel shipped two days *before* it was ordered has a perfectly ordinary date. An invoice total 10% higher than its lines is a perfectly ordinary number. Catching these needs **business rules**, and nobody has written them down.

**Who does what:**
1. **The AI proposes.** It gets the table and column names, types, row counts, null rates and number ranges, plus the relationships we discovered. **It never sees row values** (no names, emails, or text of any kind). It answers with rules like *"shipments.shipped_at ≥ orders.order_date (via ord_id → order_id)"*, each with a confidence and a one-sentence reason.
2. **The engine checks the proposal.** Do these tables and columns exist? Can a date be compared with that column? Does the join follow a relationship we actually know? Anything that fails is thrown out and listed, never stored.
3. **A person decides.** `aide rule approve ID` or `aide rule reject ID --note "we pre-ship"`. A rejected rule is never proposed again.
4. **The engine enforces.** Every night, each approved rule runs as **plain SQL**, with no AI: *"8 rows in shop.shipments break the rule … (IDs attached)"*.

Rules can only take three safe shapes: compare two columns (also across a relationship), compare a column with a number, and "a total equals the sum of its lines". The AI can't write SQL, so a bad suggestion can at most be a wrong rule that a person rejects.

**Unusual individual rows** ([design](../design/row_outliers.md)), with no AI involved: every night the database itself works out each amount or quantity column's typical value. It reports rows that are **both** at least 20× that **and** far outside the column's normal spread (*"3 rows in shop.order_items.quantity are far above normal (up to 500x the typical 1)"*), with their IDs. The values never leave the database.

**Two more kinds of AI help** ([design](../design/ai_assist.md)), both advice only:
- **Second opinion on unsure links.** When discovery is only "probably" sure two tables are linked (confidence 0.6–0.9), the AI looks at the same evidence a person would and says *likely*, *unlikely* or *unsure*, with a reason. It sorts your review list; it never confirms anything itself.
- **Plain-language explanations.** `aide explain shopco` adds what a finding means, what it can break (connected tables), likely causes and what to check. Strict rule: **the explanation may only quote numbers that are in the evidence.** If the AI invents one (*"about 2,300 orders a week"*), the explanation is thrown away.

**Provider:** switchable. Gemini first (`AIDE_LLM_PROVIDER=gemini` + key in `.env`), with others plugging in behind the same interface. With no key, every other feature still works.

---

## 7. More databases, copies, and the database itself (Stage 3)
*Design: [stage3_databases.md](../design/stage3_databases.md) · setup: [databases.md](../setup/databases.md)*

- **Any SQL database.** MySQL, SQL Server, Oracle and Snowflake share one adapter. A small table of per-database differences covers how to make the session read-only, where the row estimates and the query log live, and three function names. Everything above the adapter (discovery, detection, rules, outliers, alerts) is the same engine.
- **Does the copy match?** Pair a source with its copy (`aide reconcile add app-vs-dw --left app --right dw`). Every night the two latest scans are compared table by table: *"dw.orders has 7 fewer rows than shop.orders"*, *"NULLs 0.00% vs 8.00% in addresses.postal_code"*. If rows are missing, that's one finding, not twenty (column totals would all differ too).
- **Is the database itself healthy?** (Postgres for now.) It flags big indexes nobody uses, tables full of dead rows, slow repeated queries, sessions stuck in an open transaction, and ID sequences close to running out. It never shows query text that could contain values.

---

## The test lab: how we know it works
*Design: [test_lab.md](../design/test_lab.md)*

ShopCo is a fake retailer database built to be messy **on purpose**:
- a modern shop app
- an old billing system with UPPERCASE names and no foreign keys
- a nightly reporting job

The lab can simulate months of business in minutes, then **plant** 12 kinds of problems, and it writes down exactly what it planted (the *answer key*) *before* our detector runs. The **scorer** then compares our findings with the answer key:
- caught
- missed
- false alarms
- knock-on effects (a problem that causes another; not penalised)
- known day-one issues

That's how every number in this guide was measured, and it's the benchmark every future change must keep passing.

---

## Where we are: how much is left

| Stage | What | Status |
|---|---|---|
| 0 | Setup: tools, tests, CI | ✅ |
| 1.1 | Metadata store (the tool's memory) | ✅ |
| 1.2 | ShopCo test lab + benchmark | ✅ |
| 1.3 | Postgres scanning (read-only, in-database measurements) | ✅ |
| 1.4 | Relationship discovery + auto-docs | ✅ 15/15 |
| 1.5 | Detection, 12 checks | ✅ 11/11 caught |
| 1.6 | Lifecycle, health score, alerts, `aide run` | ✅ lab: 0 false alarms, quiet nights silent |
| 2.1–2.2 | AI proposes business rules (*"ship date must be after order date"*, *"invoice total = sum of lines"*); humans approve; the engine checks them as plain SQL | ✅ Gemini proposed 12 of 13 hidden rules; approved rules caught both planted problems |
| 2.3–2.4 | AI second opinion on unsure relationships; plain-language explanations with a number guard | ✅ tests pass; first Gemini run pending |
| 2.5 | Unusual individual rows (*"3 order lines have quantity 500; normally 1–3"*), computed inside the database, IDs attached | ✅ caught on the lab, 0 false alarms |
| 3 | **More databases** (MySQL, SQL Server, Oracle, Snowflake through one generic adapter); **source-vs-copy reconciliation**; **database health** for Postgres | code written, lint clean; tests next |
| 4 | Product: web UI, API, accounts, self-hosted Docker package, onboarding | |
| 5 | Beta with 1–3 real companies; accuracy track record | |
| 6 | Root cause ("why did this happen?") and suggested fixes | |
| 7 | Launch: website, pricing, billing | |

**In effort terms:**
- **Stage 1 (the engine) is essentially complete**, and it's the hardest technical core.
- Stage 2 is next and adds the AI layer.
- Stages 3–4 are mostly engineering breadth (more databases, a UI).
- Stages 5–7 are about real customers.
- The first **sellable demo** (a free "Database Health Audit": map + problems + score) is possible **right after Stage 1**, using the CLI.

---

## Glossary
| Term | Meaning |
|---|---|
| **Metadata store** | Our own database: what we know about the customer's databases |
| **Scan** | One nightly read of structure + measurements |
| **Profile** | The measurements of a table or column from one scan |
| **Baseline / normal** | A column's usual values, learned from its last 14 scans |
| **Finding** | One detected problem, with numbers and evidence |
| **Event vs condition** | Something that *happened* (stays open) vs something *currently true* (resolves when fixed) |
| **Relationship** | A link between tables (declared FK or discovered) |
| **Quiet baseline** | First run: record everything, alert nothing |
| **Answer key** | The lab's written list of planted problems, made before detection runs |
| **Business rule** | An expectation about the data (*"shipped after ordered"*). AI may propose one; only a person can approve it |
| **Proposed / active rule** | Waiting for review / approved and checked every night |
