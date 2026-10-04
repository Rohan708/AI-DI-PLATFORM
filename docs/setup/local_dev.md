# Local development setup (Windows)

## Prerequisites
- **Python 3.12.** We use the conda env `py312`.
- **Docker Desktop.** Needed by the testcontainers integration tests and by `docker compose`. Download it from docker.com and enable the WSL 2 backend. Check with `docker version`.
- **make** (optional). Install with `winget install ezwinports.make`. Every Make target is a plain command, listed below, so you can run them without make.
- **git**

## First-time setup
Run these from the repo root:

```bash
conda activate py312
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
pre-commit install
copy .env.example .env
```

In `.env`, set `POSTGRES_PASSWORD` to a value of your choice and use the same password inside `AIDE_DATABASE_URL`.

## Everyday commands
| make target | Plain command | What it does |
|---|---|---|
| `make lint` | `ruff check .` and `ruff format --check .` | Lint and check formatting |
| `make format` | `ruff check --fix .` and `ruff format .` | Auto-fix |
| `make typecheck` | `mypy` | Strict type check (src + tests) |
| `make test-unit` | `pytest -m "not integration"` | Fast tests, no Docker |
| `make test` | `pytest` | All tests (integration needs Docker) |
| `make check` | lint + typecheck + test | Same as CI |
| `make db-up` / `db-down` | `docker compose up -d` / `down` | Local Postgres on :5432 |
| `make migrate` | `alembic upgrade head` | Create/upgrade the metadata-store tables in `AIDE_DATABASE_URL` |
| `make migration m="..."` | `alembic revision --autogenerate -m "..."` | Generate a migration after changing models |

When Docker isn't running locally, integration tests are **skipped** with a reason. That is not the same as passing. In CI they **fail** instead of skipping.

## Looking at the local database
After `docker compose up -d` and `alembic upgrade head`, you can inspect the tables with:

```bash
docker exec -it aide-postgres psql -U aide -d aide -c "\dt"
```

## Rules to remember
- Never commit `.env` or `*.p8`. `.gitignore` and the `detect-private-key` pre-commit hook guard against this.
- Keep private keys and credentials **outside** the repo.
- ruff's `DTZ` rules reject naive datetimes. Use `datetime.now(timezone.utc)`.
