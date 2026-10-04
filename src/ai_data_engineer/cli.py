"""Command-line entry point: ``aide``.

``aide lab ...`` drives the test lab. Stage 1 adds ``scan``, ``detect``, ``score``, ``alert``.
"""

import argparse
from collections.abc import Sequence

from ai_data_engineer import __version__
from ai_data_engineer.lab.cli import add_lab_parser, run_lab_command


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aide", description="AI Data Engineer CLI")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("version", help="print the installed version")
    add_lab_parser(subcommands)

    args = parser.parse_args(argv)
    if args.command == "version":
        print(__version__)
    elif args.command == "lab":
        return run_lab_command(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
