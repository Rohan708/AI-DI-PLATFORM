"""``aide reconcile ...``: pairs of sources that should hold the same data."""

import argparse

from sqlalchemy import select

from ai_data_engineer.graph.models import DataSource, ReconciliationPair
from ai_data_engineer.ingestion.cli import store_session
from ai_data_engineer.ingestion.sources import get_source
from ai_data_engineer.reconcile.run import PairConfigError, add_pair, get_pair, reconcile


def add_reconcile_parser(
    subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    rec = subcommands.add_parser("reconcile", help="compare a source with its copy")
    commands = rec.add_subparsers(dest="reconcile_command", required=True)
    p = commands.add_parser("add", help="pair a source (left) with its copy (right)")
    p.add_argument("name", help="a name for the pair, e.g. app-vs-warehouse")
    p.add_argument("--left", required=True, help="the original (data source name)")
    p.add_argument("--right", required=True, help="the copy (data source name)")
    p.add_argument(
        "--schema-map", action="append", default=[], metavar="LEFT=RIGHT",
        help="the copy uses another schema name, e.g. shop=analytics (repeatable)",
    )  # fmt: skip
    p.add_argument(
        "--table", action="append", default=[], metavar="LEFT=RIGHT",
        help="compare only these tables, e.g. shop.orders=dw.fct_orders (repeatable)",
    )  # fmt: skip
    commands.add_parser("list", help="all pairs")
    p = commands.add_parser("run", help="compare now (from the latest scans of both)")
    p.add_argument("name")
    p = commands.add_parser("remove", help="delete a pair (its findings stay)")
    p.add_argument("name")


def run_reconcile_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        if args.reconcile_command == "add":
            settings: dict[str, object] = {}
            if args.schema_map:
                settings["schema_map"] = _pairs(args.schema_map, "--schema-map")
            if args.table:
                settings["tables"] = _pairs(args.table, "--table")
            pair = add_pair(
                session, args.name, get_source(session, args.left),
                get_source(session, args.right), settings,
            )  # fmt: skip
            print(f"added pair {pair.name}: {args.left} -> {args.right} {pair.settings or ''}")
        elif args.reconcile_command == "list":
            pairs = select(ReconciliationPair).order_by(ReconciliationPair.name)
            for pair in session.scalars(pairs):
                left = session.get_one(DataSource, pair.left_source_id).name
                right = session.get_one(DataSource, pair.right_source_id).name
                print(f"{pair.name:24} {left} -> {right}  {pair.settings or ''}")
        elif args.reconcile_command == "run":
            result = reconcile(session, get_pair(session, args.name))
            print(result.summary())
            for ref in result.unmatched:
                print(f"  no copy found for {ref}")
        else:
            session.delete(get_pair(session, args.name))
            print(f"removed pair {args.name}")
    return 0


def _pairs(values: list[str], flag: str) -> dict[str, str]:
    out = {}
    for value in values:
        left, sep, right = value.partition("=")
        if not sep or not left or not right:
            raise PairConfigError(f"{flag} expects LEFT=RIGHT, got {value!r}")
        out[left.strip()] = right.strip()
    return out
