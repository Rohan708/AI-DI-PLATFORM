# Detection (Stage 1.5)

*Code: `src/ai_data_engineer/detection/`. Commands: `aide detect`, `aide findings`. Settings: `detection/settings.py`.*

Detection turns the history we collect on every scan into **findings**. Every check is deterministic, with no AI involved, and each table and column is judged against **its own history**: there are no fixed thresholds about anyone's data.

```
aide detect shopco
  load: current structure, the latest scan, the 14 scans before it, relationships
  run every check  ->  observations
  record           ->  new finding / refresh the open one (no duplicates)
  resolve          ->  conditions that were re-checked and are fine again
```

---

## 1. Events vs conditions
| Kind | Meaning | Lifecycle |
|---|---|---|
| **event** | something *happened*: a column was dropped, a type changed | stays open until a person resolves it; the change doesn't "un-happen" |
| **condition** | something *is currently true*: NULL rate abnormally high, table stale | resolved automatically once a later run re-checks the subject and it's fine again |

A condition is only resolved if its subject was actually **evaluated** this run. A check that is still learning never resolves anything.

## 2. Learning "normal"
- **Baseline:** the last **14** scans (`baseline_scans`).
- **Cold start:** with fewer than **7** earlier scans (`min_history_scans`), statistical checks report *"still learning"* instead of guessing. `aide detect` prints how many measurements are still learning.
- **Robust statistics:** median and MAD (median absolute deviation) instead of mean and standard deviation, so one odd day in the history doesn't distort "normal":
  ```
  robust_z = (value − median(history)) / max(1.4826 × MAD(history), min_spread)
  ```
  The `min_spread` floor stops a perfectly flat history (0%, 0%, 0% …) from making a 0.01% wobble look infinitely unusual.
- **Two conditions, not one:** a value must be both *statistically* unusual **and** *practically* meaningful (e.g. at least +2 percentage points of NULLs).

## 3. The checks

### Structural (from the version history; no learning needed)
| Check | Kind | Fires when | Severity |
|---|---|---|---|
| `column_removed` | event | a column disappeared in the last 7 days | high |
| `type_changed` | event | a column's type changed: **high** if the type *family* changed (number → text), **low** if only details changed (`varchar(20)` → `varchar(50)`) | high / low |
| `nullability_changed` | event | a NOT NULL rule was added or removed | low |
| `primary_key_removed` | event | a table lost its primary key | high |
| `missing_primary_key` | condition | a table has *never* had a primary key | medium |
| `unindexed_foreign_key` | condition | a relationship's child columns (declared or discovered) aren't the leading columns of any index, so joins and parent deletes scan the whole table | low |

### Column values (each column against its own history)
| Check | Fires when | Severity |
|---|---|---|
| `null_rate_spike` | NULL share ≥ 2 points above its median **and** ≥ 4 robust-z | medium (high at +20 points) |
| `out_of_range` | negative values in a column that was never negative, or a maximum ≥ 10× the largest ever seen | medium |
| `inconsistent_categories` | a low-cardinality text column whose set of values was **stable** for the whole baseline gets new values. Flagged as **variants** when they look like an existing value (`USA` / `United States` vs `US`: same after normalising, a prefix, or initials). | medium |
| `duplicate_values` (category: *relational*, i.e. duplicate entities) | a column that was unique in every baseline scan now has duplicates, including values that differ **only by letter case** (`ANNA@X.COM` vs `anna@x.com`, via the case-insensitive distinct count measured in-database) | high |

### Time series (table-level history)
| Check | Fires when | Severity |
|---|---|---|
| `volume_drop` | rows added since the previous scan < **60%** of the usual, compared with the **same weekday** when there is at least 1 such scan (weekends are naturally quieter). Skips tables that normally gain < 5 rows, and tables that sometimes shrink (full refreshes). | high |
| `stale_table` | a timestamp/date column that moved forward in ≥ 80% of earlier scans didn't move this time: the job that fills the table probably didn't run. Uses the most regular such column; one finding per table. | high |

Every finding's **title and description quote the actual numbers**: baseline, current value, threshold, and number of scans used. The **evidence** JSON holds everything needed to verify it.

## 4. Settings (`DetectionSettings`)
| Setting | Default |
|---|---|
| `baseline_scans` / `min_history_scans` | 14 / 7 |
| `event_lookback_days` | 7 |
| `robust_z_threshold` | 4.0 |
| `null_rate_min_spread` / `null_rate_min_increase` / `null_rate_high_increase` | 0.005 / 0.02 / 0.2 |
| `magnitude_jump_factor` | 10 |
| `volume_drop_ratio` / `volume_min_baseline_rows` / `min_same_weekday_samples` | 0.6 / 5 / 1 |
| `freshness_min_regularity` | 0.8 |
| severities | per check, see tables above |

## 5. Also in this stage (fixes from 1.4)
- **Query-log memory:** joins seen in `pg_stat_statements` are stored in our metadata store (`query_join` table, migration `0002`), keeping the largest call count ever observed. A Postgres restart no longer erases "the application joins these columns".
- **"Not re-checked" marker:** if a relationship's confidence falls below the orphan-check threshold, its open orphan finding stays open but records `rechecked: false` and the reason, instead of silently looking current.
- **New measurement:** a case-insensitive distinct count for text columns, stored in `column_profile.extra.distinct_case_insensitive`.

## 6. Benchmark scoring changes
- **Known baseline issues:** ShopCo has real problems from day one (`INV_LINE_TAX` has no primary key; 12 relationship columns have no index). They're listed in the answer key and reported as *"known baseline issues found"*, not as false alarms. They're matched first, and by check name.
- **Knock-on findings** count as *related*, not false alarms: findings on an anomaly's related tables, on the same column (e.g. orphan IDs also make `cust_no`'s maximum jump), or on the same table in the same category (duplicated customers also duplicate their phone numbers).

## 7. Done when
On the planted lab (`standard` plan, nightly scans, then `discover` and `detect`):
- every Stage-1 planted problem caught (11 scenarios / 11 answer-key entries; business rules wait for Stage 2)
- at most **2** false alarms, each explainable
- known baseline issues found (at most 1 missed)

The integration test `test_benchmark_catches_every_stage_one_anomaly` asserts exactly this.

## 8. Known limitations
- Severity is per check, not scaled by business impact (1.6 adds impact via relationships).
- Freshness relies on timestamp/date columns; tables without one can't be judged yet (activity counters are stored for a later fallback).
- Volume only detects drops, not spikes.
- Variant matching is lexical (prefix, initials, normalisation). Synonyms like `Deutschland` / `DE` need AI review (Stage 2).

## 9. Results log
| Date | Run | Stage-1 caught | False alarms | Known issues | Notes |
|---|---|---|---|---|---|
| 2026-10-07 | lab `standard`, size `small`, seed 42, 15 nightly scans, then `discover` + `detect` | **11 / 11** | 1 | 13 / 13 | Precision 93%, 7 knock-on findings. False alarm: `shop.shipments` volume on a Monday (Saturday orders, quieter) compared with the all-days median, because only 1 earlier Monday existed and 2 were required. **Fixed:** `min_same_weekday_samples` 2 → 1. |
