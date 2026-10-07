"""``aide source ...`` and ``aide scan ...`` commands."""

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ai_data_engineer.config import get_settings
from ai_data_engineer.db import create_db_engine
from ai_data_engineer.graph.models import RunStatus, SourceKind
from ai_data_engineer.ingestion.scan import scan_source
from ai_data_engineer.ingestion.sources import add_source, get_source, list_sources
from ai_data_engineer.ingestion.summary import render_source_summary


def add_ingestion_parsers(
    subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    source = subcommands.add_parser("source", help="register and inspect monitored databases")
    commands = source.add_subparsers(dest="source_command", required=True)

    p = commands.add_parser("add", help="register a database to monitor")
    p.add_argument("name")
    p.add_argument("--kind", choices=[k.value for k in SourceKind], default="postgres")
    p.add_argument(
        "--connection-ref",
        required=True,
        help="NAME of the environment variable (or .env entry) holding the connection URL",
    )
    p.add_argument("--include-schemas", default="", help="comma-separated; default: all")
    p.add_argument("--exclude-schemas", default="", help="comma-separated")
    p.add_argument("--sample-row-threshold", type=int)
    p.add_argument(
        "--no-value-samples",
        action="store_true",
        help="never store actual values (top values, text min/max)",
    )

    commands.add_parser("list", help="list registered databases")
    p = commands.add_parser("show", help="what we know about a database (latest scan)")
    p.add_argument("name")

    scan = subcommands.add_parser("scan", help="scan a registered database now")
    scan.add_argument("name")
    scan.add_argument("--as-of", help="record the scan at this ISO datetime (testing/lab)")


def run_source_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        if args.source_command == "add":
            settings: dict[str, object] = {}
            if args.include_schemas:
                settings["include_schemas"] = _split(args.include_schemas)
            if args.exclude_schemas:
                settings["exclude_schemas"] = _split(args.exclude_schemas)
            if args.sample_row_threshold:
                settings["sample_row_threshold"] = args.sample_row_threshold
            if args.no_value_samples:
                settings["allow_value_samples"] = False
            source = add_source(
                session,
                name=args.name,
                kind=SourceKind(args.kind),
                connection_ref=args.connection_ref,
                settings=settings,
            )
            print(f"registered {source.name} ({source.kind.value}) settings={source.settings}")
        elif args.source_command == "list":
            for source in list_sources(session):
                print(f"{source.name:20} {source.kind.value:10} ref={source.connection_ref}")
        elif args.source_command == "show":
            print(render_source_summary(session, get_source(session, args.name)))
    return 0


def run_scan_command(args: argparse.Namespace) -> int:
    observed_at = _parse_as_of(args.as_of) if args.as_of else None
    with store_session() as session:
        result = scan_source(session, get_source(session, args.name), observed_at=observed_at)
        print(f"scan of {args.name}: {result.summary()}")
        for table, error in result.errors.items():
            print(f"  ! {table}: {error}")
    return 0 if result.status is RunStatus.SUCCEEDED else 1


@contextmanager
def store_session() -> Iterator[Session]:
    """A metadata-store session that commits on success."""
    engine = create_db_engine(get_settings().database_url.get_secret_value())
    try:
        with Session(engine) as session, session.begin():
            yield session
    finally:
        engine.dispose()


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_as_of(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
