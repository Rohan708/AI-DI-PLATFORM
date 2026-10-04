"""Command-line entry point: ``aide``.

Stage 1 adds ``ingest``, ``detect``, ``score`` and ``alert`` subcommands here.
"""

import argparse
from collections.abc import Sequence

from ai_data_engineer import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aide", description="AI Data Engineer CLI")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("version", help="print the installed version")

    args = parser.parse_args(argv)
    if args.command == "version":
        print(__version__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
