# Design: the API (Stage 4.2)

**Status:** built and tested 2026-10-17: unit tests, integration tests on the test lab, and a `docker compose up` run end to end.
**Code:** `api/` (`app.py`: token check, errors, server; `routes.py`: endpoints; `views.py`: rows to responses; `schemas.py`: shapes). **Interactive docs:** `/api/docs` on a running install.

## 1. Goal
Everything a person does in the terminal (look at findings, confirm or reject them, approve rules, answer questions) becomes available over HTTP. The web app (4.4) is built on it, and customers can use it from their own scripts.

## 2. Decisions
| | Decision |
|---|---|
| **Framework** | FastAPI + uvicorn (core dependencies). Plain synchronous endpoints, one database session per request. |
| **Where it runs** | inside `lumida serve`, next to the scheduler (one container), or alone with `lumida api`. |
| **Off by default** | it starts only when `LUMIDA_API_TOKEN` is set. An empty value counts as not set. |
| **Login** | one shared token: `Authorization: Bearer <token>`, compared in constant time. Personal accounts and roles replace it in **4.3**. |
| **Who did it** | review requests carry `by` (required), written to the audit trail exactly like `--by` on the command line. In 4.3 it comes from the login instead. |
| **Same rules as the CLI** | every endpoint calls the same functions: allowed status changes, rule validation, how answers become checks. Nothing is re-implemented. |
| **Read-only towards customers** | the API reads and writes only Lumida's own metadata store. It never connects to a customer database, and it never starts a run. |
| **Health** | `GET /health` returns the score stored by the last run; it is never recomputed on request. |
| **No secrets out** | sources show the *name* of the setting that holds their URL (`SHOP_DB_URL`), never the URL. |
| **Errors** | `{"detail": "..."}` with the same one-line message the CLI prints. 401 = no or wrong token. 404 = not found. 409 = not allowed (e.g. `confirmed → open`, a duplicate rule). 422 = malformed input. |
| **Network** | in the image it listens on port 8000; compose publishes it **on the host's localhost only**. To reach it from other machines, put a TLS reverse proxy in front. |
| **Versioned** | everything under `/api/v1`. |

## 3. Endpoints
| Method + path | What |
|---|---|
| `GET /sources` | every database: schedule, last run, health score, open findings by severity |
| `GET /sources/{name}` | one database |
| `GET /sources/{name}/runs?limit=` | recent nightly runs (status, times, counts, error) |
| `GET /sources/{name}/health` | score for the source, each schema and each table (worst first), plus the previous score |
| `GET /sources/{name}/findings?status=&severity=&category=&limit=&offset=` | findings, most severe first. `status`: active (default: open + confirmed), open, confirmed, rejected, resolved, all |
| `GET /findings/{id}` | one finding: description, evidence, AI explanation, review history |
| `POST /findings/{id}/review` | `{action: confirm/reject/resolve/reopen, by, note}` |
| `GET /sources/{name}/rules?status=` | business rules (AI-proposed, from answers, or written by people) |
| `POST /sources/{name}/rules` | `{spec, by}`: a person's rule, checked against the structure and active at once |
| `GET /rules/{id}` | one rule, with its review history |
| `POST /rules/{id}/review` | `{action: approve/reject/disable/enable/reconsider, by, note}` |
| `GET /sources/{name}/relationships?include_rejected=` | discovered links between tables (declared or inferred, confidence, signals, AI opinion) |
| `POST /relationships/{id}/review` | `{action: confirm/reject, by}` |
| `GET /sources/{name}/questions?limit=` | the most useful unanswered questions, best first, each with a suggested answer |
| `POST /sources/{name}/answers` | `{key, value, by}`: the answer becomes a check, a deadline, a closed day or an importance level |

Example:
```bash
curl -H "Authorization: Bearer $LUMIDA_API_TOKEN" "http://127.0.0.1:8000/api/v1/sources/shop/findings?severity=high"
```

## 4. Tests
- **Unit:** the API is off without a token, including an empty one; every endpoint answers 401 without the token or with a wrong one; the list of endpoints is fixed.
- **Integration:** one nightly run on the test lab, then through the API:
  - sources, runs and health
  - findings: filter, order, paging
  - reviewing a finding, with its audit trail and a refused status change
  - adding and reviewing rules (a duplicate rule and a malformed spec are refused)
  - rejecting a relationship
  - answering a question
  - the API as a real server next to the scheduler
- **End to end:** `docker compose up` with a token set; checked 200 with the token, 401 without, `/api/docs` loading, a source added from the CLI showing up in the API, and a clean stop.

## 5. Not yet
- **Accounts, roles, an audit log of logins, tenant isolation** (4.3).
- **"Run now" and adding or removing databases** over the API. These are still done in the terminal; they come with onboarding (4.5).
- **Starting AI work from the API** (rule proposals, explanations). The terminal only for now, because those calls take minutes and need a job queue.
- **Rate limiting and CORS.** The web app will be served from the same address, so CORS isn't needed.
