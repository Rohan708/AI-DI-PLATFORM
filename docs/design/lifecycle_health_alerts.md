# Findings lifecycle, health score, alerts & `aide run` (Stage 1.6)

*Code: `detection/recording.py` (lifecycle), `health_score/`, `alerting/`, `pipeline.py`. Commands: `aide run`, `aide health`, `aide finding …`, `aide source alerts|remove`. Tables: `finding_event`, `health_snapshot` (migration `0003`).*

This stage turns the engine into something you can **run every night and trust**: people stay in control of findings, there's one number for "how healthy is this database", and the right people hear about **new** problems without being flooded.

---

## 1. Findings lifecycle

```
           detected                      person
  (new) ──────────────► OPEN ──────────► CONFIRMED ──┐
                         │  ▲               │        │ resolved (person, or
                 rejected│  │reopen         │        │ automatically when a
                         ▼  │               ▼        ▼ condition is gone)
                      REJECTED          RESOLVED ────┘
                    ("normal here")       │  ▲
                         ▲                └──┘ reopen
                         └── un-reject (back to OPEN)
```

| Rule | Why |
|---|---|
| **Rejected = "this is normal here".** The same problem (same fingerprint) is **never raised again**; the rejected finding only records that it was seen (`last_detected_at`), and `aide detect` reports it as *suppressed*. | This is how the tool learns each database's quirks and gets quieter over time |
| **Conditions resolve themselves** when a later run re-checks them and they're fine. **Events** ("column removed") stay open until a person resolves them. | A dropped column doesn't "un-happen" |
| **Subject removed:** an open condition finding about a table/column that no longer exists is resolved with that reason. | Avoids zombie findings |
| **Every change is recorded** in `finding_event`: who (`aide` for automatic changes, otherwise the reviewer), when, from → to, note. | The audit trail, and later the visible accuracy record (Stage 5) |
| Invalid moves are refused, e.g. a rejected finding can only be reopened, not resolved. | Keeps history meaningful |

```
aide findings shopco                       # open findings with short ids
aide finding show 3fa2c1d0                 # details, evidence (JSON), full history
aide finding confirm 3fa2c1d0 --note "real, ticket DATA-42"
aide finding reject  91b0e7aa --note "legacy table, PK not needed"
aide finding resolve 5c7d...  |  aide finding reopen 5c7d...
```

## 2. Health score

```
table score  = max(0, 100 − Σ penalty of its open findings)
               penalty: critical 40 · high 20 · medium 10 · low 3 · info 0
schema score = average of its tables   (+ the worst table, stored alongside)
source score = average of all tables   (+ the worst table)
```

- Only **open/confirmed** findings count. Rejected and resolved ones don't.
- The **worst table** is always kept next to the average, so one broken critical table can't hide behind 40 healthy ones.
- A snapshot is stored for every table, schema and the source after each run (`health_snapshot`), so **trends** are visible: `aide health shopco` shows `health 87.4/100 (−6.2 since last time)`.
- `aide health` uses the latest scan's time, so the lab's simulated calendar stays consistent.

Example (ShopCo after the planted problems): `shop.orders` has a volume drop (high, −20), a type change (low, −3), an orphan finding (medium, −10), an out-of-range key (medium, −10) and two unindexed FKs (low, −6), so 100 − 49 = **51**. A table with nothing open scores **100**.

## 3. Alerts

```
aide source alerts shopco --webhook-ref AIDE_SHOPCO_SLACK   # Slack (env var holds the URL)
aide source alerts shopco --console                         # print instead (default)
```

| Rule | Detail |
|---|---|
| **Quiet baseline** | The **first** alerting run on a source sends **nothing**. Every open finding is marked "already seen" and the source's `baseline_completed_at` is stamped. *"quiet baseline: 34 existing findings recorded, not alerted"*. A messy legacy database would otherwise flood the channel on day one, and people mute it forever. |
| **Only new problems** | Each finding is alerted at most once (`alerted_at`). A problem that comes back after being resolved is a new finding and alerts again. |
| **Threshold** | **medium and above** (`AlertSettings.min_severity`). Low findings (e.g. unindexed FKs) stay in `aide findings` and the docs. |
| **One digest per run** | *"AI Data Engineer: 19 new problems in shopco (13 high, 6 medium)"* plus up to 15 lines, most severe first, each with its numbers. **3 or more findings of the same check collapse into one line**, e.g. *"[high] 8 x volume_drop: shop.orders gained 15 rows; usually about 41 (and 7 more like it)"*, so one incident spilling into many tables doesn't push everything else out. |
| **Retry on failure** | If Slack can't be reached, nothing is marked alerted; the next run retries. |
| **Secrets by reference** | The source stores the *name* of the variable holding the webhook URL, never the URL. Only `https://` webhooks are accepted. |

Implementation: `Notifier` protocol with `SlackNotifier` (standard-library HTTP, no new dependency) and `ConsoleNotifier`. Email and PagerDuty can be added behind the same protocol.

## 4. `aide run`: the nightly job

```
aide run shopco [--no-alert] [--as-of 2026-03-17T03:00]
  scan  ->  discover  ->  detect  ->  health  ->  alert
```

- Each step **commits on its own**, so a failure later never loses earlier work.
- If the **scan fails** (database unreachable, credentials missing), the rest is **skipped**, because it would only re-judge yesterday's data. The failed run stays on record.
- Every step's outcome is printed, and the **exit code is 0 only if every step succeeded**, so a scheduler (cron, Windows Task Scheduler, Kubernetes CronJob) can alert on failure.

```
aide run shopco: ok
  ok  scan      succeeded: 14 tables seen, 14 profiled
  ok  discover  4 declared, 11 inferred (…); findings: 1 new, …
  ok  detect    33 new findings, 0 still open, 0 resolved
  ok  health    health 72.1/100 (−24.3 since last time); worst table 51
  ok  alert     alerted 19 new findings
```

### Same-day re-runs are safe
Detection thins every history to **one scan per day**: when two scans are less than 20 hours apart (`time_series_min_gap_hours`), only the later counts. A second `aide run` on the same day therefore **re-judges that day**. Volume doesn't suddenly look like "0 new rows", and today's anomaly isn't absorbed into "normal".

### In the lab
```
aide lab run standard --size small --pipeline shopco3
```
This runs the whole job after every simulated night and prints each night's alert line, so you see the real story:
- night 1: *quiet baseline*
- quiet nights after that
- the injection night: one digest with the planted problems

## 5. Removing a source
```
aide source remove shopco --yes
```
This deletes everything the metadata store knows about the source (scans, structure, measurements, relationships, findings and their history, scores, remembered joins), in dependency order. **The customer's database is never touched.** Useful for re-running the lab without inventing new source names.

## 6. Done when
On a fresh lab with the nightly pipeline (integration test `test_nightly_pipeline_tells_the_right_story`):
- night 1 sends **no** alerts
- the clean nights send at most one
- the injection night sends **exactly one** digest that names the planted problems
- re-running that night sends **nothing new**
- the health score **drops**
- a rejected finding stays rejected

## 7. Known limitations
- One alert channel per source; per-owner routing needs ownership data (planned with the UI, Stage 4).
- Health penalties are per severity, not scaled by how much depends on a table (impact weighting via relationships is a candidate improvement).
- Resolved-finding notifications ("all clear") aren't sent yet.
