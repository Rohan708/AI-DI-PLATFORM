# Architecture

*This is a living document; each stage fills in its section as it's built. The full design rationale is in [`product/vision_and_roadmap.md`](product/vision_and_roadmap.md) §6.*

## Data flow
```
Customer DBs (Postgres, MySQL, MSSQL, Snowflake, …) — read-only
        │  SQL runs INSIDE the DB; aggregates + offending row IDs come out
        ▼
   ingestion/<adapter>  ──►  common adapter interface
                                     │
                                     ▼
                  graph/  (metadata store in our Postgres: identity + version
                           + profile tables, relationships, rules, findings)
                                     │
        ┌───────────────┬────────────┼───────────────┬──────────────┐
        ▼               ▼            ▼               ▼              ▼
   discovery       detection    health_score     reasoning (AI)   fixes
 (relationships)  (deterministic)                 Stage 2+         later
        └───────────────┴──► Finding table ◄─────────┘
                                  │
                                  ▼
                     alerting · docs generation · api/UI (Stage 4)
```

## Layers
| Package | Responsibility | Stage | AI allowed? |
|---|---|---|---|
| `graph` | Metadata store schema, versioning, graph queries | 1.1 | No |
| `lab` | Messy test lab (fake company DB), anomaly injector, answer key, benchmark scorer | 1.2 | No |
| `ingestion` | Adapter interface + Postgres adapter (read-only introspection, in-DB profiling, DB health) and the generic SQL adapter for MySQL / SQL Server / Oracle / Snowflake, scan orchestration, source registry — see [design](design/postgres_adapter.md), [Stage 3](design/stage3_databases.md) | 1.3, 3 | No |
| `discovery` | Relationship discovery (names, value overlap, query-log joins), orphan / parent-uniqueness checks, auto-documentation — see [design](design/relationship_discovery.md) | 1.4 | No (AI review in Stage 2) |
| `detection` | Structural, column-value and time-series checks against each column's own history (robust statistics, cold-start safe), finding recording with dedup/resolve — see [design](design/detection.md); row-level outliers computed in-database ([design](design/row_outliers.md)) | 1.4–1.5, 2.5 | No |
| `health_score` | Table/schema/source scores from open findings, stored as snapshots — see [design](design/lifecycle_health_alerts.md) | 1.6 | No |
| `alerting` | One digest per run (Slack or console), quiet baseline, alert-once | 1.6 | No |
| `reconcile` | Source-vs-copy comparison from both sides' scans — see [design](design/stage3_databases.md) | 3 | No |
| `pipeline` | `aide run`: scan → discover → rules → rows → dbhealth → detect → reconcile → health → alert | 1.6 | No |
| `rules` | Safe rule format, storage + human review, nightly checks compiled to read-only SQL — see [design](design/ai_rules.md) | 2 | No |
| `reasoning` | LLM providers (Gemini first, switchable); privacy-safe context; rule proposals, second opinions on unsure relationships, guarded finding explanations ([design](design/ai_assist.md)); later root cause | 2, 6 | **Yes** (confidence + evidence; human-reviewed) |
| `api` | FastAPI backend | 4 | — |
| `fixes` | Human-approved fix proposals | 6 | Yes (reviewed) |
| `cost` | Warehouse cost diagnosis | later | No |

Key decisions are recorded in [`decisions/`](decisions/).
