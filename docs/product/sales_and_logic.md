# Selling it & how it works

*Companion to [`vision_and_roadmap.md`](vision_and_roadmap.md). Part 1 is for selling and design-partner conversations. Part 2 explains the engine's logic in plain language, with no code.*

---

# Part 1 — Selling it

## Who buys, and why now
**Buyer:** the head of engineering or data at a company with **50–1,000 employees**. They have one or more databases that grew over years, some nightly ETL jobs, and nobody who fully understands it all.

**Moments when they're ready to buy:**
- A wrong report led to a bad decision ("revenue was off by 12% for a month").
- A **migration** is coming (to the cloud, or a new system) and they're afraid of what they'll find.
- A **new data or engineering lead** inherits a mess and needs a map.
- An **audit or compliance** request: "where is customer data, and is it correct?"

## The seven factors that sell it (most important first)

| # | Factor | Why it matters | How we deliver it |
|---|---|---|---|
| 1 | **Time to first value** | Buyers judge in the first hour | Point it at a database and get a **map + top problems within ~30 minutes**, with no rules to write |
| 2 | **Works on *their* mess** | Competitors assume clean, documented cloud warehouses | Built for legacy and transactional databases: finds **hidden relationships**, odd naming, missing keys |
| 3 | **Safe** | Security says "no" before anyone says "yes" | Read-only, self-hosted (data never leaves their network), light load on production, AI opt-in |
| 4 | **Few false alarms** | One week of noisy alerts and they mute it forever | Each table is compared with **its own** history, quiet first run, learns from "not a problem" feedback |
| 5 | **Evidence, not opinions** | Engineers don't trust black boxes | Every finding shows the numbers: "0.03% → 2.0% null, threshold 3σ, 41 rows, here's the query" |
| 6 | **Impact + why** | "Something's wrong" isn't enough | "This breaks 3 tables and the finance report; it started right after Tuesday's change" |
| 7 | **Visible track record** | Proves it's getting smarter | "92% of findings confirmed by your team this month" |

## The pitch, at three lengths
- **One line:** "Point it at any database, however messy. It tells you what's wrong, what it breaks, and why."
- **Elevator:** "Your database has problems nobody knows about: broken links between tables, silent data corruption, failed nightly loads. Existing tools need clean cloud setups or hand-written rules. We connect read-only, map everything including relationships nobody documented, learn what normal looks like, and show you every anomaly with evidence. AI suggests the rules; your team approves them."
- **Proof line:** "On our benchmark database we catch 47 of 50 planted problems with 2 false alarms." The number comes from the test lab (Stage 1.2), which is why the lab matters for sales as well as engineering.

## Go-to-market: the free "Database Health Audit"
Run the tool once against a prospect's database and hand them a report:
- a map of the database
- hidden relationships
- the top 20 problems
- a health score

This works with the **Stage 1.6 command-line version**, before any UI exists. It:
- gets you in the door cheaply
- creates the "wow" moment
- converts naturally to "monitor this continuously" (the subscription)

It's also the best way to recruit **design partners**.

## Pricing (to validate with partners, not decided)
- **Free:** one-time audit report.
- **Subscription per monitored database**, in tiers by size (number of tables) and features:
  - basic: structure + quality
  - pro: AI rules, relationship discovery, root cause
  - enterprise: SSO, audit log, support
- Self-hosted enterprise deals priced annually.

## Objections and answers

| They say | You answer |
|---|---|
| "We can't let a tool touch production." | Read-only user (we provide the setup script), runs inside their network, query time limits, can point at a replica. |
| "We already have dbt tests / Great Expectations." | Those check only what someone thought to write. We find what nobody thought of, and can export approved rules into those tools. |
| "We have Datadog." | Datadog watches whether the database is *running*. We watch whether the data inside is *correct*. |
| "AI makes things up." | AI only **suggests**. A human approves every rule, and the checks themselves are plain SQL with evidence. |
| "Will it spam us?" | The first run is silent. It learns each table's normal and stops flagging what you mark as fine. |

## Competitors in one breath
- **Monte Carlo, Metaplane, Bigeye, Anomalo:** strong, but built for **clean cloud warehouses**.
- **Great Expectations, Soda, dbt tests:** need **humans to write every rule**.
- **Datadog, pganalyze:** **performance**, not data correctness.
- **Our gap:** messy and transactional databases, hidden relationship discovery, AI-proposed rules with human approval.

---

# Part 2 — How it works (the logic, no code)

## The pipeline
```
Connect (read-only) → Scan structure → Measure data → Save to history
      → Discover relationships → Run checks → Findings → Score → Alert
```

### 1. Scan the structure
Every database keeps a catalog: a list of its own tables, columns, types and keys. We read it, which is cheap and touches no customer data.
> *Result: "orders has 9 columns; `cust_no` is an integer; there's no foreign key on it."*

### 2. Measure, inside their database
We send **one summary query per table**. The database does the counting and returns only numbers, never the rows:
```sql
SELECT COUNT(*), COUNT(*) - COUNT(cust_no),  -- rows, nulls
       COUNT(DISTINCT cust_no), MIN(total), MAX(total)
FROM orders  -- on big tables: a 1% random sample
```
> *Result: 50,000 rows; `cust_no` 12 nulls (0.02%); 4,812 distinct customers; total ranges 0.50–9,800.*

### 3. Save to history: "ID card + photos"
Think of each column as a person (this is what Stage 1.1 built):
- **Identity** is the ID card. It never changes, so everything that refers to it stays valid.
- **Versions** are official changes, like a name change. A row is added only when the *structure* changes (e.g. a type goes from number to text). That's how we spot **schema drift**.
- **Profiles** are a daily photo. A new measurement every scan, and this time series is what we learn "normal" from.

### 4. Learn "normal" and spot what's unusual
For each measurement we look at its recent history:
```
null rate, last 7 days:  0.0  0.1  0.0  0.0  0.1  0.0  0.0 (%)
average ≈ 0.03%,  typical spread ≈ 0.05%
today: 2.0%  →  (2.0 − 0.03) / 0.05 ≈ 39 "spreads" away  →  very unusual → finding
```
- The rule is "**how far from its own normal**", not "more than 5% nulls". That's why one piece of code works on every column of every company.
- **Seasonality:** compare Mondays with Mondays, because order volume on a Sunday is naturally lower.
- **Cold start:** with fewer than about 5 measurements we say "still learning" rather than guess.
- The same logic covers **row counts** (volume drop), **last update time** (stale table) and **distinct counts** (sudden duplicates).

### 5. Discover hidden relationships
Example: is `orders.cust_no` secretly a link to `customers.id`? We collect evidence and combine it into a confidence score:

| Evidence | Finding | Weight |
|---|---|---|
| Name similarity | `cust_no` → "customer number" ≈ `customers.id` | weak |
| Types match | both integers | filter only |
| Values overlap | 99.7% of `cust_no` values exist in `customers.id` | strong |
| Parent is unique | `customers.id` has no duplicates | required |
| Query logs | the app runs `JOIN customers ON cust_no = id` thousands of times | very strong |

The result is **confidence 0.94 → "proposed"**, and a human confirms it. Once confirmed we can run **orphan checks**: "0.3% of orders point to customers that don't exist (148 orders, IDs attached)."

### 6. Run the checks
| Check type | Logic |
|---|---|
| Structural | Simple rules on the catalog: "table has no primary key", "the same `customer_id` concept is INT here and TEXT there" |
| Drift | Compare today's structure version with yesterday's: "column `email` was dropped" |
| Statistical | The "far from its own normal" logic from step 4 |
| Integrity | SQL counting orphans or duplicates using discovered relationships |
| Business rules | Approved rules turned into SQL, e.g. `COUNT(*) WHERE ship_date < order_date` |

### 7. Findings: no duplicates, no spam
- Each finding gets a **fingerprint** (check + what it's about). If the same problem appears again tomorrow, we **update** the existing finding instead of creating a new one.
- Status flow: **open → confirmed / rejected → resolved**. A rejection teaches the system that this is normal for this table.
- **Severity** depends on the type and size of the problem, and how much is connected downstream.

### 8. Health score
Start at 100 and subtract for each open problem, weighted by severity, with a floor of 0. Then roll up from table to schema to whole database. The worst table is also kept visible, so one broken critical table can't hide behind a good average.

### 9. Where AI comes in (Stage 2)
```
We give the AI: table structures + measurements (+ a few sample rows, only if allowed)
AI proposes:    "orders.ship_date should be ≥ order_date (confidence 0.88, because…)"
Human:          approves or rejects
Engine:         turns the approved rule into SQL and checks it on every run, with no AI involved
```
AI is used for **imagination**: finding rules and relationships nobody wrote down. **Enforcement** is always plain, repeatable SQL. That split is what makes it trustworthy and cheap to run.

### 10. Safety built into every step
- read-only database user
- every query has a time limit
- sampling on big tables
- results are only numbers and row IDs
- runs inside the customer's network
- nothing is ever written to their database

---

**In short:** you sell *time to first value, works on messy databases, safe, evidence-backed, few false alarms*. The engine delivers that by **measuring inside their database → remembering history → comparing each thing with its own normal → discovering hidden links → AI suggesting rules a human approves**.
