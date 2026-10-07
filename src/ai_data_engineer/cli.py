"""Command-line entry point: ``aide``.

``source``/``scan`` (1.3), ``discover``/``relationships``/``docs`` (1.4), ``lab``.
"""

import argparse
from collections.abc import Sequence

from ai_data_engineer import __version__
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
from ai_data_engineer.lab.cli import add_lab_parser, run_lab_command


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aide", description="AI Data Engineer CLI")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("version", help="print the installed version")
    add_ingestion_parsers(subcommands)
    add_discovery_parsers(subcommands)
    add_lab_parser(subcommands)

    args = parser.parse_args(argv)
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
    elif args.command == "lab":
        return run_lab_command(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
