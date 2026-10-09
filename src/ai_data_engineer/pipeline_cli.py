"""``aide run`` (the whole nightly job) and ``aide health`` commands."""

import argparse
from datetime import UTC, datetime

from ai_data_engineer.health_score.engine import compute_health
from ai_data_engineer.ingestion.cli import store_session
from ai_data_engineer.ingestion.sources import get_source
from ai_data_engineer.pipeline import run_pipeline


def add_pipeline_parsers(
    subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    p = subcommands.add_parser(
        "run", help="the nightly job: scan, discover, rules, rows, dbhealth, detect, reconcile, "
        "health, alert"
    )
    p.add_argument("name", help="data source name")
    p.add_argument("--no-alert", action="store_true", help="do everything except alerting")
    p.add_argument("--as-of", help="record the run at this ISO datetime (testing/lab)")
    p = subcommands.add_parser("health", help="health score of a source, its schemas and tables")
    p.add_argument("name")


def run_run_command(args: argparse.Namespace) -> int:
    observed_at = None
    if args.as_of:
        parsed = datetime.fromisoformat(args.as_of)
        observed_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    with store_session() as session:
        result = run_pipeline(
            session,
            get_source(session, args.name),
            observed_at=observed_at,
            alert=not args.no_alert,
        )
    print(result.summary())
    return 0 if result.ok else 1


def run_health_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        report = compute_health(session, get_source(session, args.name))
        print(f"{args.name}: {report.summary(top=10)}")
    return 0
