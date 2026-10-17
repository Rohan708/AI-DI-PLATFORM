# Install Lumida (self-hosted)

Lumida runs as two containers inside your network: **Lumida itself** and **its own small Postgres** (the metadata store: what it has learned about your databases). It connects **read-only** to the databases you register and runs every one of them each night at its own time.

## 1. Requirements
- Docker with Compose (Docker Desktop, or Docker Engine + the compose plugin)
- About 1 GB of memory and a few GB of disk for the metadata store
- Network access from the Lumida container to the databases you want to watch
- A **read-only login** on each of those databases: [`databases.md`](databases.md) (MySQL, SQL Server, Oracle, Snowflake) and `scripts/postgres/create_readonly_user.sql` (Postgres)

## 2. Configure
```bash
cd deploy
cp .env.example .env        # Windows: copy .env.example .env
```
Edit `.env`:
- `POSTGRES_PASSWORD`: a long random string, letters and digits only.
- **One URL per database you want to watch**, under any name, e.g.
  ```
  SHOP_DB_URL=postgresql+psycopg://lumida_readonly:<password>@db-host:5432/shop
  ```
- Optional: a Slack webhook (`SLACK_DATA_ALERTS=https://hooks.slack.com/...`) and the AI settings.

`.env` holds secrets. Keep it on the server, out of version control.

## 3. Start
```bash
docker compose up -d --build
docker compose ps
```
Both services should show **healthy** within a minute. On start, Lumida brings its metadata store up to date by itself.

## 4. Add your databases
```bash
docker compose exec lumida lumida source add shop --connection-ref SHOP_DB_URL --run-at 03:00 --timezone Europe/Berlin
docker compose exec lumida lumida source alerts shop --webhook-ref SLACK_DATA_ALERTS
```
- `--run-at` / `--timezone` choose when it runs each night (default 03:00 UTC). Change them later with `lumida source schedule shop --run-at 02:30`, or stop scheduling with `--off`.
- A new database waits for its first scheduled time. To see results right away, run it once by hand:
  ```bash
  docker compose exec lumida lumida run shop
  docker compose exec lumida lumida findings shop
  ```
- The first run records what's already wrong without alerting ("quiet baseline"). Alerts start with the next run.

## 5. Day to day
| | |
|---|---|
| What it found | `docker compose exec lumida lumida findings shop` · `lumida finding show <id>` |
| Help it learn | `docker compose exec -it lumida lumida ask shop` (a few questions about your data) |
| Health of the service | `docker compose exec lumida python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read().decode())"` |
| Logs | `docker compose logs -f lumida` (JSON lines; never row values) |

**When a nightly run fails** (a database unreachable, a password changed), Lumida sends an alert about *that*, on the database's own channel, and tries again the next night.

The health endpoint answers **200** while the scheduler is alive, with each database's last and next run. A database that fails doesn't make the service unhealthy; that's alerted instead. To reach `/healthz` from a monitoring tool, set `LUMIDA_HEALTH_HOST=0.0.0.0` and publish port 8080.

## 6. The API (optional)
Lumida has an HTTP API: the same things as the commands above, for scripts and (soon) the web app. It's **off until you set a token** in `deploy/.env`:
```
LUMIDA_API_TOKEN=<a long random string>
```
To generate one: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Then run `docker compose up -d` and open **http://127.0.0.1:8000/api/docs** (click *Authorize* and paste the token). Every request sends `Authorization: Bearer <token>`:
```bash
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8000/api/v1/sources
```
The API is published on this machine only. To reach it from other machines, put your own reverse proxy with TLS in front of it. Endpoints: [`docs/design/api.md`](../design/api.md).

## 7. Upgrade
```bash
git pull                      # or download the new release
docker compose up -d --build
```
Migrations run automatically on start. A run in progress finishes its current database before the old container stops (the stop timeout is 5 minutes).

## 8. Back up
Everything Lumida knows (history, findings, your answers and rules) is in the `lumida-metadata` volume:
```bash
docker compose exec metadata pg_dump -U lumida lumida > lumida-backup.sql
```
Your own databases are never written to, so there's nothing to back up on their side.

## 9. Remove
```bash
docker compose down        # keeps the metadata volume
docker compose down -v     # also deletes everything Lumida learned
```

## Notes
- **Included drivers:** Postgres, MySQL, Oracle, Snowflake. SQL Server needs Microsoft's ODBC driver and its licence; a separate image variant follows.
- **No outside connections are needed**, except to your databases, to Slack if you use it, and to the AI provider only if you turn AI features on.
