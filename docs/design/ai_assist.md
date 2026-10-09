# Design: AI relationship review and finding explanations (Stage 2.3–2.4)

**Status:** ✅ tests pass (2026-10-11, fake LLM); first Gemini run pending.
**Builds on:** the provider switch and privacy rules of [ai_rules.md](ai_rules.md) (§5–6).
**Code:** `reasoning/review_relationships.py`, `reasoning/explain.py`, `reasoning/replies.py`.

Both features are **advice**. Neither changes a status, opens or closes a finding, or runs at check time.

## 1. AI review of unsure relationships (2.3)
**Problem:** discovery proposes links with a confidence. At 0.9 and above we trust a link enough to run orphan checks. Between 0.6 and 0.9 it's "probably": a person has to look, and in a big legacy database that's a long list.

**What the AI gets** for each unsure link (inferred, still `proposed`, confidence < `orphan_check_min_confidence`):
- both sides' table, column names, native types, row counts, distinct and NULL counts
- discovery's own evidence: inclusion (share of child values found in the parent), query-log join calls, name score, distinctive values, which parent key, and the reasons
- **no row values**

**What it answers:** `likely` / `unlikely` / `unsure`, a confidence (0–1) and a one-sentence reason. Answers with an unknown id, an unknown verdict or a bad confidence are counted as unusable and ignored.

**Where it goes:** `relationship.evidence["ai_review"]` holds the verdict, confidence, reason, provider, model, time, and the **SHA-256 of the evidence it saw**.
- Nightly discovery refreshes the measurements but **keeps** `ai_review` (`KEPT_EVIDENCE_KEYS`).
- When the measurements change, the hash no longer matches, so the link is due for review again. `--again` forces a re-review.

**Commands:**
```
aide relationships review shopco          # ask; prints the opinions, clear ones first
aide relationships list shopco            # undecided links show  [AI: likely 0.85]
aide relationships confirm|reject ID      # a person decides, as before
```

## 2. Finding explanations (2.4)
**Problem:** a finding is precise (*"NULLs in shop.addresses.postal_code jumped from 0.0% to 8.0%"*) but doesn't say what it means for the business or where to look first.

**Input** (`finding_context`):
| Sent | Not sent |
|---|---|
| the finding's check, category, severity, status, title, description, dates, evidence | `sample_rows` (offending row identifiers) |
| the table: rows, primary key, column names; the subject column's type, NULL and distinct counts, and numeric/date min-max | text values: for `inconsistent_categories` the title, description and values become counts |
| connected tables (relationships in and out, with status) | |
| up to 10 other open findings on this or connected tables, so a common cause shows up | |

**Output:** `summary`, `impact`, up to 3 `likely_causes`, up to 3 `next_steps`, and a `confidence`.

**The number guard (done-when for 2.4):** every number in the answer must match a number in the input. Fractions may appear as percentages (0.08 → 8%). There's a 2% tolerance for rounding ("about 41" for 40.6), and 0, 1, 2 and 100 are always allowed. **One unknown number and the explanation is thrown away** and reported as rejected. The prompt tells the model not to calculate new numbers, and the guard enforces it.

**Where it goes:** `finding.evidence["explanation"]`, with provider, model, time and **the title it explained**. The finding's own `confidence` stays NULL (it's deterministic).
- Nightly refreshes keep the explanation (`KEPT_EVIDENCE_KEYS` in `recording.py`).
- Titles quote the numbers, so if the title has changed, `aide finding show` marks the explanation "*written for an earlier version of this finding*", and `aide explain` picks the finding up again.

**Commands:**
```
aide explain shopco --limit 5     # the worst open findings without an up-to-date explanation
aide finding explain ID           # one finding
aide finding show ID              # includes the explanation
```

## 3. Not in this step
- Explanations aren't added to Slack digests yet (one LLM call per finding per night costs money and adds latency). Later this could be an option for high severity only.
- Synonym detection for categories (`Deutschland` / `DE`) needs the values themselves, so it waits for the per-source opt-in to share sample values (backlog).
- Row-level outliers (2.5) need a design decision; see the open questions in the status update.

## 4. Results log
| Date | Run | Relationship reviews | Explanations kept / rejected | Notes |
|---|---|---|---|---|
| | | | | |
