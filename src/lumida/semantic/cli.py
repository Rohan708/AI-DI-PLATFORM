"""``lumida pii NAME``: where personal data lives in a source, and where it shouldn't."""

import argparse

from sqlalchemy import select

from lumida.graph.models import ACTIVE_FINDING_STATUSES, Finding
from lumida.ingestion.cli import store_session
from lumida.ingestion.sources import get_source
from lumida.semantic.classify import same_kind_different_names
from lumida.semantic.patterns import KINDS_BY_NAME
from lumida.semantic.run import PII_UNEXPECTED, check_semantic


def add_semantic_parser(
    subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    p = subcommands.add_parser(
        "pii", help="map personal data (counted inside the database) and flag stray copies"
    )
    p.add_argument("name", help="data source name")


def run_pii_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        source = get_source(session, args.name)
        result = check_semantic(session, source)
        print(f"personal data in {args.name}: {result.summary()}")
        if result.mapped:
            print("\nColumns that hold personal data:")
            for f in sorted(result.mapped, key=lambda f: (f.kind, f.table, f.column)):
                label = KINDS_BY_NAME[f.kind].label
                print(f"  {f.table}.{f.column:30} {label} ({f.share:.0%} of {f.checked:,})")
        groups = same_kind_different_names(result.mapped)
        if groups:
            print("\nSame kind of data under different names:")
            for kind, where in groups.items():
                print(f"  {KINDS_BY_NAME[kind].label}: {', '.join(where)}")
        stray = session.scalars(
            select(Finding).where(
                Finding.data_source_id == source.id,
                Finding.check_name == PII_UNEXPECTED,
                Finding.status.in_(ACTIVE_FINDING_STATUSES),
            )
        ).all()
        if stray:
            print("\nPersonal data where the column's name doesn't say so:")
            for finding in stray:
                print(f"  {str(finding.id)[:8]}  {finding.severity.value:6} {finding.title}")
    return 0
