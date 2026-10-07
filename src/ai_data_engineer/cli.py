"""Command-line entry point: ``aide``.

``source``/``scan`` (1.3), ``discover``/``relationships``/``docs`` (1.4), ``lab``.
"""

import argparse
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from ai_data_engineer import __version__
from ai_data_engineer.detection.cli import (
    add_detection_parsers,
    run_detect_command,
    run_findings_command,
)
from ai_data_engineer.discovery.cli import (
    add_discovery_parsers,
    run_discover_command,
    run_docs_command,
    run_relationships_command,
)
from ai_data_engineer.ingestion.cli import (
    add_ingestion_parsers,
    run_scan_command,
    run_source_command,
)
from ai_data_engineer.ingestion.connections import ConnectionRefError
from ai_data_engineer.ingestion.sources import SourceExistsError, SourceNotFoundError
from ai_data_engineer.lab.cli import add_lab_parser, run_lab_command
from ai_data_engineer.lab.runner import UnknownScenarioError
from ai_data_engineer.lab.schema import LabSafetyError
from ai_data_engineer.lab.state import LabNotBuiltError

# Mistakes a user can make: reported as one line, not a traceback.
USER_ERRORS = (
    SourceExistsError,
    SourceNotFoundError,
    ConnectionRefError,
    LabNotBuiltError,
    LabSafetyError,
    UnknownScenarioError,
    ValidationError,
    LookupError,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aide", description="AI Data Engineer CLI")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("version", help="print the installed version")
    add_ingestion_parsers(subcommands)
    add_discovery_parsers(subcommands)
    add_detection_parsers(subcommands)
    add_lab_parser(subcommands)

    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except USER_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "version":
        print(__version__)
    elif args.command == "source":
        return run_source_command(args)
    elif args.command == "scan":
        return run_scan_command(args)
    elif args.command == "discover":
        return run_discover_command(args)
    elif args.command == "relationships":
        return run_relationships_command(args)
    elif args.command == "docs":
        return run_docs_command(args)
    elif args.command == "detect":
        return run_detect_command(args)
    elif args.command == "findings":
        return run_findings_command(args)
    elif args.command == "lab":
        return run_lab_command(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
