# Design: self-hosted package and scheduler (Stage 4.1)

**Status:** built and tested 2026-10-14. That covers unit tests, integration tests, a local image build, and a `docker compose up` run end to end.
**Code:** `service/` (schedule maths, scheduler, health endpoint, migrations on start, `lumida serve` / `lumida migrate`), `Dockerfile`, `.dockerignore`, `deploy/` (compose file + `.env.example`). **Guide:** [`docs/setup/install.md`](../setup/install.md).

## 1. Goal
A company installs Lumida inside its network with one command. After that it runs every database every night by itself, says when its own run failed, and can be checked as healthy, without anyone typing `lumida run`.

## 2. Pieces
| Piece | Decision |
|---|---|
| **Image** | Python 3.12 slim, a wheel built in a separate stage, a non-root user (uid 10001). Extras: `mysql,oracle,snowflake` (build arg `EXTRAS`). The test lab (Faker) isn't included; `lumida lab` says how to add it. 409 MB. |
| **Migrations** | shipped inside the package (`package-data`); `lumida serve` upgrades the store on start (`lumida migrate` does only that). Works from the wheel or image, without `alembic.ini`. |
| **Schedule** | per source: `run_at` (local `HH:MM`, default `03:00`) + `timezone` (default `UTC`), stored with the source's settings. `None` = not scheduled. Set with `source add --run-at/--timezone` or `source schedule NAME --run-at ... / --off`. |
| **Due** | the most recent scheduled moment has passed and nothing ran since. After downtime, each source runs **once** (no catch-up of every missed night). A new source waits for its first scheduled moment. |
| **Daylight saving** | a skipped local time runs at the first valid minute after the jump; a repeated one runs once, at its first occurrence. |
| **One at a time** | sources run sequentially, so a customer's databases never see several Lumida jobs at once. |
| **Never twice** | a Postgres advisory lock per source (`pg_try_advisory_lock` on the metadata store). A second container, or a manual run, skips a source that's already running. |
| **Never in a loop** | an attempt counts as a run even when it failed before recording anything (e.g. a missing connection URL), so a broken source is retried the next night, not every minute. |
| **Self-monitoring** | a failed run is alerted on the source's own channel: *"Lumida: the nightly run for shop failed"*, with the failing step. A broken alert channel is logged; it doesn't stop the scheduler. |
| **Health** | `GET /healthz` returns 200 while the loop ticks; 503 if it hasn't ticked for 3 intervals. The body has each source's schedule and its last and next run: names and times only, never connection details or data. It binds to 127.0.0.1 by default. Docker's `HEALTHCHECK` uses it. |
| **Logs** | stdout; `LUMIDA_LOG_FORMAT=json` (the image default) gives one JSON object per line. |
| **Stopping** | SIGTERM / Ctrl+C sets a stop flag; the run in progress finishes its source. The compose file gives it 5 minutes. |

## 3. Settings
| Setting | Default |
|---|---|
| `LUMIDA_SCHEDULER_TICK_SECONDS` | 60 |
| `LUMIDA_HEALTH_HOST` / `LUMIDA_HEALTH_PORT` | 127.0.0.1 / 8080 (0 = off) |
| `LUMIDA_LOG_FORMAT` | text (json in the image) |
| per source `run_at` / `timezone` | 03:00 / UTC |

## 4. Tests
- **Unit:**
  - schedule maths (time zones, the DST gap and overlap in Berlin, downtime, new sources)
  - schedule validation
  - health statuses (ok / degraded / stalled)
  - JSON log format
- **Integration** (each on a fresh database migrated from the packaged files):
  - a due source runs once and a not-yet-due one waits
  - a failing source alerts once and isn't retried the same night
  - a locked source is skipped
  - `serve --once` works, and `/healthz` answers without exposing connection details
- **Image:** CI builds it and runs `lumida version` on every push. Locally verified with `docker compose up`, which showed migrations applied, both services healthy, `/healthz` ok, and a source added with its own schedule.

## 5. Not yet
- **SQL Server image variant** (Microsoft ODBC driver + licence acceptance).
- **Publishing the image** to `ghcr.io/rohan708/lumida`, when a design partner needs it.
- **Cron expressions** (several runs a day, weekdays only); daily times cover v1.
- **Overlap protection across sources** within one database server (two sources on the same server run one after another anyway).
- **Web UI** (4.4). The API (4.2) runs in the same container: [`api.md`](api.md).
