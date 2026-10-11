# Design: context from people (Stage 2.7)

**Status:** code written and tested 2026-10-11 (unit + lab integration).
**Code:** `context/` (store, questions, dbt import, CLI), the new rule kinds in `rules/spec.py`, `freshness_deadline` and closed-day handling in `detection/checks/timeseries.py`, criticality weights in `health_score/`.

## 1. Why
The engine learns *what the data does*; it can't know *what the business intends*. Learned baselines have two blind spots:
1. **They need history.** For about a week, a new column is "still learning".
2. **They take broken for normal.** A column that was always wrong looks normal.

A few answers from the people who own the database close both gaps, and make the AI's proposals and explanations better.

## 2. Ask a few smart questions, don't make people fill forms
`aide ask mydb` asks the top 10 unanswered questions in the terminal. Each is generated from what the engine measured and pre-filled with its best guess (press Enter to accept, `?` for don't know). `aide questions` / `aide answer KEY VALUE` do the same without the prompt.

| Question | Asked when | A "yes" (or a value) becomes |
|---|---|---|
| Closed on some weekdays? | once per source | no volume / staleness alarms for those days; excused in deadlines |
| Does `A.x` point to `B.y`? | an inferred link below 0.9 confidence (shows the AI's opinion if any) | relationship confirmed, so orphan checks start (or rejected) |
| Should `x` always be unique? | unique today, not enforced; text, or integer named like an id or code | a `unique` rule |
| Must `x` always be filled? | no NULLs today, but the column allows them | a `not_null` rule |
| Are these all the allowed values? | 2–20 distinct values today (text or boolean) | an `allowed_values` rule (or type the right list) |
| How late is too late? | tables with a load/update time column (`loaded_at`, `refreshed_at`, …) | a freshness deadline |
| How important is this table? | the 5 largest tables | criticality 1 / 2 / 3 |

**The rules about asking:**
- Answered questions aren't asked again.
- "Don't know" comes back after 30 days.
- Tables under 20 rows aren't asked about (too little evidence).

## 3. What answers become
| Kind | Stored as | Used by |
|---|---|---|
| required / unique / allowed values | **rules** (origin `user`, active): the same table, review and nightly check as AI-proposed rules | the `rules` step: plain read-only SQL |
| closed weekdays | note on the source | volume, staleness and deadline checks |
| freshness deadline | note on the table (`column`, `max_age_hours`) | `freshness_deadline` check (from the first scan) |
| criticality | note on the table | health score: a critical table counts 2×, a low one 0.5× in averages |
| descriptions | notes on source / table / column | data dictionary, AI rule proposals, explanations |

**The new rule kinds** (Postgres SQL and generic SQLAlchemy, all five dialects):

| Kind | Checks |
|---|---|
| `not_null` | every row has a value |
| `unique` | no value shared by two rows (rows involved are counted) |
| `allowed_values` | non-NULL values compared as text against the list |

Only people (`aide ask`, `rules add`, dbt) create these kinds; the AI proposes only the comparison kinds.

**Wrong or outdated answers show up as findings in themselves**: *"you said email is unique; 32 duplicates"*.

**Closed days:** a scan "covers" the day 12 hours before it (a 03:00 scan covers yesterday, setting `closed_day_offset_hours`). Volume and staleness skip scans that cover a closed day, and closed-day deltas leave the volume baseline. The deadline check subtracts 24 h per closed day since the newest row.

**History:** every note keeps its history (`valid_to`). A partial unique index allows one current fact per subject and key.

## 4. Import what already exists: dbt
`aide context import-dbt mydb models/schema.yml --schema analytics` maps:

| In `schema.yml` | Becomes |
|---|---|
| model or source-table `description`, column `description` | descriptions |
| `not_null`, `unique` | rules (active, created by `dbt:schema.yml`) |
| `accepted_values: {values: [...]}` | an `allowed_values` rule |
| `relationships: {to: ref('x'), field: y}` | the discovered relationship, confirmed |

It reads `tests:` and dbt 1.8+ `data_tests:`, with arguments given directly or under `arguments:`. Re-importing is idempotent. Database column comments were already read by the scan.

## 5. Other commands
```
aide context describe mydb shop.orders.status "Order lifecycle: placed -> paid -> shipped"
aide context set mydb shop.payments --critical 3
aide context set mydb reporting.daily_sales --deadline 26 --clock loaded_at
aide context set mydb - --closed sun
aide context show mydb
```

## 6. Proof on the lab
1. **Day-one detection:** with one scan, two answers (`postal_code` must be filled; the country list is complete) catch the planted NULL spike and country variants with exact row counts. The learned checks are still "learning" after two scans.
2. **Deadlines:** `daily_sales` with a 26 h deadline isn't late an hour after its load, is late three days later, and is excused when those days are marked closed.
3. **Criticality:** marking the worst table critical lowers the source's health score.
4. **dbt:** a `schema.yml` gives 3 active rules, 2 descriptions and the `orders.cust_no → customers.id` link confirmed. An unknown model is reported, and a re-import adds nothing.

## 7. Known limitations
- No status-transition rules ("never back from paid to placed"); they need row-level change history.
- Questions are asked one source at a time in the terminal; a web form comes with Stage 4.
- The AI doesn't draft descriptions yet (planned: drafts a person corrects).
- Deadlines use the newest value of a time column, so a table that only receives updates needs an `updated_at`-style column.
