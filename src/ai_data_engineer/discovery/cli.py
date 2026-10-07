"""``aide discover``, ``aide relationships``, ``aide docs`` commands."""

import argparse
import getpass
from pathlib import Path

from sqlalchemy.orm import Session

from ai_data_engineer.discovery.catalog import load_catalog
from ai_data_engineer.discovery.discover import discover
from ai_data_engineer.discovery.docs import DEFAULT_DOCS_DIR, generate_docs
from ai_data_engineer.discovery.store import current_relationships, find_relationship, review
from ai_data_engineer.graph.models import (
    Asset,
    AssetColumn,
    Relationship,
    RelationshipKind,
    RelationshipStatus,
    utcnow,
)
from ai_data_engineer.ingestion.cli import store_session
from ai_data_engineer.ingestion.sources import get_source


def add_discovery_parsers(
    subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    p = subcommands.add_parser(
        "discover", help="find relationships (incl. undeclared FKs) and check them"
    )
    p.add_argument("name", help="data source name")

    rel = subcommands.add_parser("relationships", help="list and review relationships")
    commands = rel.add_subparsers(dest="rel_command", required=True)
    p = commands.add_parser("list", help="current relationships of a source")
    p.add_argument("name")
    p.add_argument("--all", action="store_true", help="include rejected ones")
    for verb in ("confirm", "reject"):
        p = commands.add_parser(verb, help=f"{verb} a relationship (by id prefix)")
        p.add_argument("id")
        p.add_argument("--by", default=None, help="reviewer name (default: OS user)")

    p = subcommands.add_parser("docs", help="generate a data dictionary + relationship map")
    p.add_argument("name")
    p.add_argument("--out", type=Path, default=DEFAULT_DOCS_DIR)


def run_discover_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        result = discover(session, get_source(session, args.name))
        print(f"discovery of {args.name}: {result.summary()}")
        for c in result.proposed:
            print(
                f"  + {c.child.ref}({', '.join(c.child_columns)}) -> "
                f"{c.parent.ref}({', '.join(c.parent_columns)})  confidence {c.confidence:.2f}"
            )
    return 0


def run_relationships_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        if args.rel_command == "list":
            catalog = load_catalog(session, get_source(session, args.name))
            for rel in sorted(current_relationships(session, catalog), key=lambda r: str(r.id)):
                if rel.status is RelationshipStatus.REJECTED and not args.all:
                    continue
                conf = f"{rel.confidence:.2f}" if rel.confidence is not None else "  - "
                kind = "FK " if rel.kind is RelationshipKind.DECLARED else "inf"
                print(
                    f"{str(rel.id)[:8]}  {kind}  {rel.status.value:9}  {conf}  "
                    f"{_describe(session, rel)}"
                )
        else:
            rel = find_relationship(session, args.id)
            status = (
                RelationshipStatus.CONFIRMED
                if args.rel_command == "confirm"
                else RelationshipStatus.REJECTED
            )
            review(session, rel, status, args.by or getpass.getuser(), utcnow())
            print(f"{status.value}: {_describe(session, rel)}")
    return 0


def run_docs_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        paths = generate_docs(session, get_source(session, args.name), args.out)
    for path in paths:
        print(f"wrote {path}")
    return 0


def _describe(session: Session, rel: Relationship) -> str:
    parts = []
    for pair in rel.columns:
        child = session.get_one(AssetColumn, pair.from_column_key)
        parent = session.get_one(AssetColumn, pair.to_column_key)
        parts.append((child, parent))
    child_asset = session.get_one(Asset, parts[0][0].asset_key)
    parent_asset = session.get_one(Asset, parts[0][1].asset_key)
    return (
        f"{child_asset.schema_name}.{child_asset.name}({', '.join(c.name for c, _ in parts)}) -> "
        f"{parent_asset.schema_name}.{parent_asset.name}({', '.join(p.name for _, p in parts)})"
    )
