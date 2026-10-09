"""``aide lab ...`` commands."""

import argparse
import json
from datetime import date
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ai_data_engineer.alerting.notifier import ConsoleNotifier
from ai_data_engineer.config import get_settings
from ai_data_engineer.db import create_db_engine
from ai_data_engineer.graph.models import DataSource
from ai_data_engineer.ingestion.cli import store_session
from ai_data_engineer.ingestion.scan import scan_source
from ai_data_engineer.ingestion.sources import get_source
from ai_data_engineer.lab.answer_key import AnswerKey, write_answer_key
from ai_data_engineer.lab.rule_scoring import FoundRule, load_ai_rules, score_rules
from ai_data_engineer.lab.runner import (
    PLANS,
    DayHook,
    answer_key,
    build_lab,
    inject,
    lab_scan_time,
    run_plan,
    tick,
)
from ai_data_engineer.lab.scenarios import SCENARIOS
from ai_data_engineer.lab.schema import HIDDEN_RULES
from ai_data_engineer.lab.scorer import (
    FoundFinding,
    FoundRelationship,
    load_findings,
    load_relationships,
    score,
)
from ai_data_engineer.lab.sizes import SIZES
from ai_data_engineer.lab.state import load_state
from ai_data_engineer.pipeline import run_pipeline

DEFAULT_SEED = 42
ANSWER_KEY_DIR = Path("validation/answer_keys")
REPORT_DIR = Path("validation/reports")


def add_lab_parser(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    lab = subcommands.add_parser("lab", help="the messy test lab and benchmark")
    commands = lab.add_subparsers(dest="lab_command", required=True)

    p = commands.add_parser("build", help="(re)create the lab database with history")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--size", choices=sorted(SIZES), default="default")

    p = commands.add_parser("tick", help="simulate more business days")
    p.add_argument("--days", type=int, default=1)
    _add_night_options(p)

    p = commands.add_parser("inject", help="plant anomalies (applied from the next day)")
    p.add_argument("scenarios", nargs="+", metavar="SCENARIO")

    p = commands.add_parser("run", help="build + clean days + inject + aftermath")
    p.add_argument("plan", nargs="?", choices=sorted(PLANS), default="standard")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--size", choices=sorted(SIZES), default="default")
    _add_night_options(p)

    commands.add_parser("scenarios", help="list available scenarios and plans")
    commands.add_parser("status", help="show the lab's current simulated day and injections")

    p = commands.add_parser("answer-key", help="write the answer key (JSON + Markdown)")
    p.add_argument("--out", type=Path, default=ANSWER_KEY_DIR)

    p = commands.add_parser("score", help="compare findings with the answer key")
    p.add_argument(
        "--data-source", help="metadata-store data source that scanned the lab (Stage 1.3+)"
    )
    p.add_argument("--out", type=Path, default=REPORT_DIR)


def _add_night_options(p: argparse.ArgumentParser) -> None:
    night = p.add_mutually_exclusive_group()
    night.add_argument("--scan", metavar="SOURCE", help="scan this data source after each day")
    night.add_argument(
        "--pipeline",
        metavar="SOURCE",
        help="run the full nightly job (aide run) after each simulated day",
    )


def run_lab_command(args: argparse.Namespace) -> int:
    command = args.lab_command
    if command == "scenarios":
        for scenario in SCENARIOS.values():
            print(f"{scenario.name:24} {scenario.category.value:14} {scenario.summary}")
        print()
        for plan in PLANS.values():
            print(f"plan {plan.name:10} {plan.description}")
        return 0

    engine = _lab_engine()
    try:
        if command == "build":
            state = build_lab(engine, seed=args.seed, size=args.size)
            print(f"lab built: seed={state.seed} size={state.size} through {state.current_day}")
        elif command == "tick":
            days = tick(engine, args.days, on_day_end=_night_hook(args))
            print(f"simulated {len(days)} day(s); now at {days[-1]}")
        elif command == "inject":
            for entry in inject(engine, args.scenarios):
                print(f"planted {entry.scenario}: {entry.description}")
            print("they take effect on the next simulated day (`aide lab tick`)")
        elif command == "run":
            key = run_plan(
                engine, args.plan, seed=args.seed, size=args.size, on_day_end=_night_hook(args)
            )
            _write_key(key, ANSWER_KEY_DIR)
            print(f"plan '{args.plan}' done: {len(key.anomalies)} anomalies planted")
        elif command == "status":
            with engine.connect() as conn:
                state = load_state(conn)
            print(f"seed={state.seed} size={state.size} current_day={state.current_day}")
            print(f"pending ETL faults: {state.pending_faults or 'none'}")
            print(f"planted anomalies: {len(state.injected)}")
        elif command == "answer-key":
            _write_key(answer_key(engine), args.out)
        elif command == "score":
            return _score(engine, args.data_source, args.out)
    finally:
        engine.dispose()
    return 0


def _night_hook(args: argparse.Namespace) -> DayHook | None:
    """What runs after each simulated day: the full pipeline (``--pipeline``), just a scan
    (``--scan``), or nothing."""
    if args.pipeline:
        return _pipeline_hook(args.pipeline)
    return _scan_hook(args.scan)


def _pipeline_hook(source_name: str) -> DayHook:
    """The whole nightly job (``aide run``) after each simulated day, as if scheduled."""

    def hook(day: date) -> None:
        with store_session() as session:
            result = run_pipeline(
                session,
                get_source(session, source_name),
                observed_at=lab_scan_time(day),
                notifier=ConsoleNotifier(),
            )
        status = "ok" if result.ok else "FAILED"
        alert = next((s.detail for s in result.steps if s.step == "alert"), "-")
        print(f"  {day}: run {status}; {alert}")
        if not result.ok:
            print(result.summary())

    return hook


def _scan_hook(source_name: str | None) -> DayHook | None:
    """Scan ``source_name`` after each simulated day, stamped with the simulated time."""
    if source_name is None:
        return None

    def hook(day: date) -> None:
        with store_session() as session:
            result = scan_source(
                session, get_source(session, source_name), observed_at=lab_scan_time(day)
            )
        print(f"  {day}: scan {result.summary()}")

    return hook


def _lab_engine() -> Engine:
    url = get_settings().lab_database_url
    if url is None:
        raise SystemExit("AIDE_LAB_DATABASE_URL is not set (see .env.example)")
    return create_db_engine(url.get_secret_value())


def _write_key(key: AnswerKey, directory: Path) -> None:
    name = f"lab-seed{key.seed}-{key.size}-{key.current_day.isoformat()}"
    json_path, md_path = write_answer_key(key, directory, name)
    print(f"answer key: {md_path} (and {json_path.name})")


def _score(lab_engine: Engine, data_source: str | None, out: Path) -> int:
    key = answer_key(lab_engine)
    findings: list[FoundFinding] = []
    relationships: list[FoundRelationship] = []
    ai_rules: list[FoundRule] = []
    if data_source:
        store = create_db_engine(get_settings().database_url.get_secret_value())
        try:
            with Session(store) as session:
                source = session.scalar(select(DataSource).where(DataSource.name == data_source))
                if source is None:
                    raise SystemExit(f"no data source named {data_source!r} in the metadata store")
                findings = load_findings(session, source.id)
                relationships = load_relationships(session, source.id)
                ai_rules = load_ai_rules(session, source.id)
        finally:
            store.dispose()
    else:
        print("no --data-source given: scoring an empty result (nothing scans the lab yet)\n")

    report = score(key, findings, relationships)
    markdown, as_json = report.to_markdown(), report.to_json()
    if ai_rules:  # Stage 2: how well the AI's rule proposals cover the hidden rules
        rules = score_rules(HIDDEN_RULES, ai_rules)
        markdown += "\n" + rules.to_markdown()
        as_json["rules_proposed"] = len(rules.found)
        as_json["rules_hidden"] = len(rules.found) + len(rules.missed)
        as_json["rule_proposals"] = rules.proposals
    out.mkdir(parents=True, exist_ok=True)
    name = f"benchmark-seed{key.seed}-{key.size}-{key.current_day.isoformat()}"
    (out / f"{name}.md").write_text(markdown, encoding="utf-8")
    (out / f"{name}.json").write_text(json.dumps(as_json, indent=2), encoding="utf-8")
    print(markdown)
    print(f"report: {out / name}.md")
    return 0
