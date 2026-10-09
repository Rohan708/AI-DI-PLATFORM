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

## The test lab (Stage 1.2)
`docker compose up -d` also starts `aide-lab-postgres` on port 5433, the fake company database. Set the `LAB_POSTGRES_*` and `AIDE_LAB_DATABASE_URL` values in `.env` (see `.env.example`), then:

```bash
aide source add shopco --connection-ref AIDE_LAB_DATABASE_URL --exclude-schemas aide_lab
aide lab run standard --size small --pipeline shopco   # full nightly job each simulated night
# or step by step:
aide lab run standard --size small --scan shopco
aide source show shopco
aide discover shopco
aide detect shopco
aide findings shopco
aide lab score --data-source shopco
aide docs shopco
```

Re-running a lab plan on the same source? Run `aide source remove shopco --yes` first, because scan history must move forward in time.

See [`docs/design/test_lab.md`](../design/test_lab.md) for everything else.

## Looking at the local database
After `docker compose up -d` and `alembic upgrade head`, you can inspect the tables with:

```bash
docker exec -it aide-postgres psql -U aide -d aide -c "\dt"
```

## Troubleshooting
- **`ImportError: DLL load failed … An Application Control policy has blocked this file`** (mypy, Windows): Smart App Control is blocking mypy's compiled extension. Install the pure-Python build instead (slower, same results):
  ```powershell
  python -m pip install --force-reinstall --no-deps --no-binary mypy "mypy>=1.11"
  ```
- **`Program 'aide.exe' failed to run: An Application Control policy has blocked this file`**: the same Smart App Control block, this time on the `aide.exe` launcher pip generates. Run the CLI through Python instead; it's identical:
  ```powershell
  python -m ai_data_engineer source list
  ```
  Everywhere the docs say `aide …`, you can write `python -m ai_data_engineer …`.
- **Wrong environment:** the prompt must show `(py312)`, not `(base)`. Run `conda activate py312` first.
- **`docker compose` says a password variable is missing:** your `.env` lacks a line from `.env.example` (e.g. the `LAB_POSTGRES_*` block).
- **Integration tests are skipped:** Docker Desktop isn't running (bottom-left must say "Engine running").

## Rules to remember
- Never commit `.env` or `*.p8`. `.gitignore` and the `detect-private-key` pre-commit hook guard against this.
- Keep private keys and credentials **outside** the repo.
- ruff's `DTZ` rules reject naive datetimes. Use `datetime.now(timezone.utc)`.
