# Design: AI-proposed business rules (Stage 2.1–2.2)

**Status:** ✅ tests pass (2026-10-11); first Gemini run on the lab done (see results log).
**Packages:** `reasoning/` (the only code allowed to call an LLM), `rules/` (rule format, storage, review, checks), `ingestion/postgres/rules.py` (rules compiled to SQL).

## 1. The idea in one paragraph
Some problems can't be found by statistics. A parcel shipped *before* it was ordered, or an invoice whose total doesn't match its lines, looks perfectly normal column by column. Finding these needs **business rules**, and nobody writes them down. The AI reads the database's structure and statistics, then proposes rules a domain expert would expect. A person approves or rejects each one. Approved rules then run every night as **plain SQL, with no AI involved**.

> **AI discovers, humans approve, the engine enforces.**

## 2. Flow
```
aide rules propose shopco      (once, or when the schema changes)
   │  1. context: tables, columns, types, keys, row counts, null rates, distinct counts,
   │     numeric/date min-max, known relationships, existing rules      (no row values)
   │  2. LLM (Gemini) answers in our rule format, each with confidence + rationale
   │  3. deterministic validation: tables/columns exist, types comparable,
   │     joins follow a known relationship  → invalid ones dropped and listed
   │  4. stored as `proposed` (origin ai); duplicates and rejected-before skipped
   ▼
aide rules list shopco  →  aide rule show ID  →  aide rule approve|reject ID --note "…"
   ▼
aide run shopco  (nightly: scan → discover → rules → detect → health → alert)
      the `rules` step runs every `active` rule as read-only SQL → findings (business_rule)
```

## 3. The rule format (`rules/spec.py`)
Rules are **JSON data, never SQL text**. The AI can only choose from three shapes, and anything else is refused (`extra="forbid"`, fixed operator list `< <= = >= > <>`).

| Kind | Meaning | Example |
|---|---|---|
| `compare_columns` | `column op other_column` in the same row, or in a **parent row** reached through a known relationship (`via`) | `shop.shipments.shipped_at >= shop.orders.order_date (via ord_id -> order_id)` |
| `compare_constant` | `column op number` | `shop.order_items.quantity > 0` |
| `sum_matches` | a parent's total = sum of its children's column, within `tolerance` | `legacy.INV_HDR.TOTAL_AMT = sum(legacy.INV_LINE.LINE_AMT) (via INV_NO -> INV_NO)` |

```json
{"kind": "compare_columns", "table": "shop.shipments", "column": "shipped_at", "op": ">=",
 "other_column": "order_date",
 "via": {"parent_table": "shop.orders", "on": [["ord_id", "order_id"]]}}
```

**Semantics:**
- Rows where a compared value is NULL are **not judged**, because unknown isn't a violation.
- `sum_matches` judges only parents that have at least one child.
- Every join must follow a relationship the engine knows (declared, or discovered and not rejected), with exactly those columns. This stops the AI from inventing joins that multiply rows.
- A rule's **signature** is a hash of its canonical JSON, so the same rule is never stored twice and a rejected rule is never proposed again.

## 4. Compiled SQL (`ingestion/postgres/rules.py`)
There are two read-only queries per rule, run inside the adapter's read-only, time-limited transaction:
1. `SELECT count(*) FILTER (WHERE judged), count(*) FILTER (WHERE broken) FROM …`
2. If anything is broken, the **primary-key values** of up to 20 offending rows. These are identifiers, not data.

Names are quoted with `quote_ident`, and the only values are bind parameters (`:value`, `:tolerance`). `sum_matches` uses `CROSS JOIN LATERAL (SELECT sum(…), count(*) … WHERE child.key = t.key)`, so it works on a sampled parent table too. Tables above the sampling threshold (1M rows) are read with `TABLESAMPLE` like the other checks, and the finding says so.

## 5. Privacy: what the AI sees (`reasoning/context.py`)
| Sent | Never sent |
|---|---|
| table/column names, native types, type family, primary keys | any text value: text min/max, frequent values, samples |
| row counts, null rates, distinct counts | row identifiers |
| min/max of **numeric and date** columns (aggregates; switch off with `share_numeric_ranges=False`) | connection details, credentials |
| known relationships with status + confidence; existing rules with status | |

The prompt itself isn't stored. Each proposal keeps the SHA-256 of the input it came from (`evidence.context_sha256`), plus the provider, model, rationale and confidence. A per-source opt-in to share sample values (to propose allowed-values rules, for example) is in the backlog.

## 6. Providers (`reasoning/llm.py`)
One small interface, `complete_json(system, prompt) -> LLMReply`, selected by `AIDE_LLM_PROVIDER`:

| Provider | Setting | Notes |
|---|---|---|
| `none` (default) | n/a | AI features off; everything else works |
| `gemini` | `AIDE_GEMINI_API_KEY`, optional `AIDE_LLM_MODEL` (default `gemini-2.5-flash`) | REST via the standard library; the key goes in a header, never the URL; JSON response mode; temperature 0.2 |

Adding Claude, OpenAI or a local model takes one class plus one branch in `llm_from_settings`. Tests use a fake client and a faked `urlopen`, so there are no real calls.

## 7. Review lifecycle
| From | Allowed to |
|---|---|
| `proposed` | `active` (approve), `rejected` |
| `active` | `disabled` |
| `disabled` | `active` (enable) |
| `rejected` | `proposed` (reconsider) |

Every change is appended to `rule.evidence.review_history` (who, when, from, to, note). User-written rules (`aide rules add`) are validated the same way and start `active`.

## 8. Rule findings (`rules/checks.py`)
- One finding per rule (fingerprint = rule id), category `business_rule`, check `business_rule`, `rule_id` set.
- `confidence = NULL`: the check is deterministic, even if the rule came from AI.
- The title quotes the numbers, e.g. "*8 rows in shop.shipments break the rule shop.shipments.shipped_at >= shop.orders.order_date (via ord_id -> order_id)*".
- The evidence holds the rule, the counts, the fraction, the offending primary keys, whether the table was sampled, and who approved the rule.
- **Severity:** high if at least 1% of judged rows break it, otherwise medium (`RuleSettings`).
- **Lifecycle:** with zero violations the finding is resolved. If the rule can't run (a column was dropped, or a timeout), it's marked **not re-checked** with the reason rather than silently looking current, and the other rules still run.

## 9. Settings (`RuleSettings`)
| Setting | Default |
|---|---|
| `sample_size` | 20 offending row ids |
| `high_severity_fraction` | 0.01 |
| `min_confidence` | 0.5 (proposals the AI rates lower are dropped) |
| `max_proposals` | 40 per request |
| `max_tables_in_prompt` | 200 |
| `share_numeric_ranges` | true |

## 10. Lab benchmark
`HIDDEN_RULES` in `lab/schema.py` lists 13 rules that hold in clean ShopCo data but are declared nowhere: dates in order, amounts and quantities not negative, payments equal order totals, invoice totals equal the sum of their lines. `aide lab score` adds an **AI rule proposals** section when the source has AI rules:
- **recall:** hidden rules proposed / 13
- how many proposals were approved and rejected
- proposals outside the answer key. These aren't automatically wrong, because the key can't list every true rule, so the reviewer decides.

The two Stage-2 scenarios (`ship_before_order`, `invoice_total_mismatch`) count as caught when an approved rule's finding lands on their table and column.

## 11. Done when (Stage 2 roadmap)
On the lab DB, it proposes most hidden rules with acceptable precision, and the approved rules catch the injected violations, making the benchmark **13/13**.

## 12. Known limitations
- The rule format has no expressions yet (`total_amount = sum(quantity * unit_price)`), no allowed-values or "status transition" rules (these need value sharing or history), and no conditional rules ("if status = shipped then shipped_at is not null").
- Large schemas: the prompt includes at most 200 tables, with no chunking yet.
- Rules aren't re-proposed automatically when the schema changes; run `aide rules propose` again.

## 13. Results log
| Date | Run | Hidden rules proposed | Approved / rejected | Stage-2 caught | Notes |
|---|---|---|---|---|---|
| 2026-10-10 | shopco3 (lab `standard`, small, seed 42), Gemini `gemini-2.5-flash`, two `rules propose` runs | **12 / 13** | 21 / 4 | **2 / 2** | First run: 20 proposals, all valid, but every confidence 0.98–0.99 and no child-vs-parent comparisons except `paid_at`. **Prompt tweak:** "go through every relationship and compare dates/amounts" and "use the whole confidence range". Second run: 5 more (confidences 0.75–0.99), 1 invalid (a join through no known relationship, correctly refused). Never proposed: `shipments.shipped_at >= orders.order_date`, added by hand with `aide rules add`. Rejected on review: `daily_sales.orders_count/revenue > 0` (a day can have no sales), two price-equality rules (prices change over time). 22 active rules ran with **0 false alarms**; lab benchmark 13/13. |
