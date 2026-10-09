# Design: row-level outliers (Stage 2.5)

**Status:** ✅ tested 2026-10-11 (unit + integration; lab: planted rows caught, 0 false alarms).
**Code:** `detection/rows.py` (which columns, findings), `ingestion/postgres/outliers.py` (the SQL). It runs as the `rows` step of `aide run`, or by hand with `aide outliers NAME`.

## 1. What it catches
Single rows with a value **far above everything else in their column**: an order line with quantity 500 where 1–3 is normal, an order 100× the usual total, a price typed in cents instead of dollars. Column-level checks only see that the maximum moved. This check says **how many rows** and **which ones** (their primary keys).

## 2. Decision: in the database, no ML library (yet)
We chose this on 2026-10-10. It needs no new dependency, and **no row values leave the customer's database**. The SQL computes the statistics and returns:
- counts
- the median and the cutoff (aggregates)
- the primary keys of up to 20 of the most extreme rows

Multi-column ML (e.g. IsolationForest: "this amount is unusual *for this customer*") would need row features pulled out of the database, so it comes later as a per-table opt-in.

## 3. The rule
On a **log scale** (so prices from 3 to 1,500 are judged by orders of magnitude, not dollars), over the rows with a positive value:
```
cut = ln(median) + max( 6 × robust spread , ln(20) )
robust spread = max(1.4826 × MAD(ln x), 0.1)
outlier: ln(value) > cut
```
A row must be **both** statistically extreme (more than 6 typical spreads above the median) **and** at least **20×** the median. Wide columns like order totals don't flag, because their spread is large. Narrow columns like quantity (1–3) flag only far-off values: with the cutoff at 20× the median, 500 is flagged.

Only the **high side** is checked: negative or impossible values are the `out_of_range` check's job.

| Setting (`DetectionSettings`) | Default |
|---|---|
| `outlier_sigmas` | 6.0 |
| `outlier_min_ratio` | 20 |
| `outlier_min_spread` | 0.1 |
| `outlier_min_rows` | 100 (smaller tables aren't judged) |
| `outlier_sample_size` | 20 primary keys |
| `outlier_max_columns` | 200 per run |
| `severity_row_outlier` | medium |

Tables above the sampling threshold (1M rows) are read with `TABLESAMPLE`, and the finding says so.

## 4. Which columns
Numbers that **measure** something. Skipped:
- primary keys and unique keys
- declared FK columns, and columns in any known relationship (including discovered ones)
- integer columns whose every value is distinct (ids)
- names ending in a key word after abbreviation expansion (`LINE_NO` → *line number*, `ITEM_CD` → *item code*) unless they also name a measure (`total`, `amount`, `quantity`, …)
- views

## 5. Findings
- One per column (fingerprint = column), category `row_outlier`, check `row_outlier`. It's a **condition**: it resolves when no row is that extreme any more.
- The title quotes the numbers: *"3 rows in shop.order_items.quantity are far above normal (up to 500x the typical 1)"*.
- The evidence holds the counts, median, cutoff, maximum ratio, thresholds, whether the table was sampled, and the offending primary keys.

## 6. Lab
New scenario **`fat_finger_quantity`** (Stage 2): 3 order lines get quantity 500. The Stage-1 `out_of_range` check also notices the column's maximum jumping. That counts as a knock-on finding (same column), not a false alarm. The integration test also asserts **no outliers on the clean lab**, which shows there are no false alarms on its measure columns.

## 7. Known limitations
- One column at a time: an amount that is normal overall but unusual for its customer isn't caught (the ML opt-in, later).
- High side only; values that are suspiciously *small* (a unit mix-up the other way) aren't caught yet.
- If a column is dropped, its open outlier finding isn't auto-resolved yet.

## 8. Results log
| Date | Run | Caught | False alarms | Notes |
|---|---|---|---|---|
| 2026-10-11 | lab `standard`, small, seed 42 (shopco4) | `fat_finger_quantity`: 3 / 3 rows | 0 | *"3 rows in shop.order_items.quantity are far above normal (up to 250x the typical 2)"*; no other measure column flagged |
