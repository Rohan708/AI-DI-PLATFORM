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
    p = commands.add_parser(
        "review", help="AI second opinion on unsure relationships (advice; you decide)"
    )
    p.add_argument("name")
    p.add_argument("--again", action="store_true", help="also re-review ones already reviewed")
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
                    f"{_describe(session, rel)}{_ai_opinion(rel)}"
                )
        elif args.rel_command == "review":
            # Imported here: only this command needs an LLM.
            from ai_data_engineer.config import get_settings
            from ai_data_engineer.reasoning.llm import llm_from_settings
            from ai_data_engineer.reasoning.review_relationships import review_relationships

            source = get_source(session, args.name)
            result = review_relationships(
                session, source, llm_from_settings(get_settings()), again=args.again
            )
            print(result.summary())
            catalog = load_catalog(session, source)
            reviewed = [r for r in current_relationships(session, catalog) if _ai_opinion(r)]
            for rel in sorted(reviewed, key=_review_order):
                print(f"  {str(rel.id)[:8]}  {_describe(session, rel)}{_ai_opinion(rel)}")
                print(f"            {rel.evidence['ai_review'].get('reason', '')}")
            if reviewed:
                print("decide: aide relationships confirm|reject ID")
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


def _ai_opinion(rel: Relationship) -> str:
    """``  [AI: likely 0.85]`` for undecided relationships with an AI review."""
    review = rel.evidence.get("ai_review")
    if not review or rel.status is not RelationshipStatus.PROPOSED:
        return ""
    return f"  [AI: {review['verdict']} {review['confidence']:.2f}]"


def _review_order(rel: Relationship) -> tuple[int, float]:
    """Clear opinions first (likely, then unlikely), most confident first."""
    review = rel.evidence["ai_review"]
    rank = {"likely": 0, "unlikely": 1, "unsure": 2}.get(review["verdict"], 3)
    return rank, -float(review["confidence"])


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
