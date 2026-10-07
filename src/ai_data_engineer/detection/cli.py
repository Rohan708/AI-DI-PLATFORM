"""``aide detect`` and ``aide findings`` commands."""

import argparse

from ai_data_engineer.detection.runner import detect, open_findings
from ai_data_engineer.graph.models import Severity
from ai_data_engineer.ingestion.cli import store_session
from ai_data_engineer.ingestion.sources import get_source

_SEVERITY_ORDER = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO]


def add_detection_parsers(
    subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    p = subcommands.add_parser("detect", help="run every anomaly check on the latest scans")
    p.add_argument("name", help="data source name")
    p = subcommands.add_parser("findings", help="list open findings of a source")
    p.add_argument("name")


def run_detect_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        result = detect(session, get_source(session, args.name))
        print(f"detection on {args.name}: {result.summary()}")
        for name, s in result.by_check.items():
            if s.opened or s.refreshed or s.resolved:
                print(f"  {name:24} +{s.opened} new  {s.refreshed} open  -{s.resolved} resolved")
    return 0


def run_findings_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        findings = open_findings(session, get_source(session, args.name))
        findings.sort(key=lambda f: (_SEVERITY_ORDER.index(f.severity), f.check_name))
        for f in findings:
            print(f"{f.severity.value:8} {f.category.value:13} {f.check_name:22} {f.title}")
        print(f"{len(findings)} open findings")
    return 0
