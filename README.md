# AI Data Engineer

A database-agnostic, AI-assisted anomaly detection engine. Point it at any database (transactional or warehouse, however messy) and it discovers the structure and hidden relationships, learns what normal looks like, and finds anomalies with evidence. AI proposes rules; humans approve them; the engine enforces them.

**Status:** Stage 1.1 done (metadata store); Stage 1.2 done (test lab + benchmark); Stage 1.3 (Postgres adapter) next. Read [`docs/product/vision_and_roadmap.md`](docs/product/vision_and_roadmap.md) for the full picture, and [`CLAUDE.md`](CLAUDE.md) for the current stage and rules.

## Quickstart
```bash
conda activate py312
python -m pip install -e ".[dev]"
copy .env.example .env
make check
```

For full instructions, see [`docs/setup/local_dev.md`](docs/setup/local_dev.md). The Snowflake test setup (used again in Stage 3) is in [`docs/setup/snowflake_setup.md`](docs/setup/snowflake_setup.md).

## Layout
- `src/ai_data_engineer/`: the package, one subpackage per layer (see [`docs/architecture.md`](docs/architecture.md))
- `tests/unit/`: fast tests, no Docker
- `tests/integration/`: tests that need Docker (testcontainers Postgres)
- `scripts/`: setup SQL and helper scripts
- `validation/`: the messy test lab, answer keys and benchmark (Stage 1.2), plus jaffle_shop for Snowflake
- `docs/`: the brief, architecture notes, setup guides, decision records
