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
| `ingestion` | One adapter per database type (Postgres first) | 1.3 | No |
| `discovery` *(to add)* | Relationship inference, auto-documentation | 1.4 | No (AI review in Stage 2) |
| `detection` | Deterministic checks + statistics → findings | 1.5 | No |
| `health_score` | Scores from open findings | 1.6 | No |
| `alerting` | Routing, dedup, quiet baseline | 1.6 | No |
| `reasoning` | Rule proposals, explanations, root cause | 2, 6 | **Yes** (confidence + evidence; human-reviewed) |
| `api` | FastAPI backend | 4 | — |
| `fixes` | Human-approved fix proposals | 6 | Yes (reviewed) |
| `cost` | Warehouse cost diagnosis | later | No |

Key decisions are recorded in [`decisions/`](decisions/).
