"""``aide detect``, ``aide findings`` and ``aide finding ...`` commands."""

import argparse
import getpass
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_data_engineer.detection.dbhealth import check_db_health
from ai_data_engineer.detection.recording import change_status
from ai_data_engineer.detection.rows import check_row_outliers
from ai_data_engineer.detection.runner import detect, open_findings
from ai_data_engineer.graph.models import (
    DataSource,
    Finding,
    FindingEvent,
    FindingStatus,
    Severity,
    utcnow,
)
from ai_data_engineer.ingestion.cli import store_session
from ai_data_engineer.ingestion.sources import get_source

_SEVERITY_ORDER = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO]
_ACTIONS = {
    "confirm": FindingStatus.CONFIRMED,
    "reject": FindingStatus.REJECTED,
    "resolve": FindingStatus.RESOLVED,
    "reopen": FindingStatus.OPEN,
}


def add_detection_parsers(
    subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    p = subcommands.add_parser("detect", help="run every anomaly check on the latest scans")
    p.add_argument("name", help="data source name")
    p = subcommands.add_parser(
        "outliers", help="find individual rows far above normal (computed in the database)"
    )
    p.add_argument("name", help="data source name")
    p = subcommands.add_parser(
        "dbhealth", help="database health: unused indexes, bloat, slow queries (Postgres)"
    )
    p.add_argument("name", help="data source name")
    p = subcommands.add_parser("findings", help="list open findings of a source (with ids)")
    p.add_argument("name")

    finding = subcommands.add_parser("finding", help="show or review one finding")
    commands = finding.add_subparsers(dest="finding_command", required=True)
    p = commands.add_parser("show", help="details, evidence and history of a finding")
    p.add_argument("id", help="finding id (or a unique prefix)")
    p = commands.add_parser("explain", help="AI explanation: impact, causes, next steps")
    p.add_argument("id")

    p = subcommands.add_parser("explain", help="AI explanations for the worst open findings")
    p.add_argument("name", help="data source name")
    p.add_argument("--limit", type=int, default=5, help="at most this many (default 5)")
    for action, status in _ACTIONS.items():
        p = commands.add_parser(action, help=f"mark a finding {status.value}")
        p.add_argument("id")
        p.add_argument("--note", help="why (recorded in the audit trail)")
        p.add_argument("--by", help="reviewer name (default: OS user)")


def run_detect_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        result = detect(session, get_source(session, args.name))
        print(f"detection on {args.name}: {result.summary()}")
        for name, s in result.by_check.items():
            if s.opened or s.refreshed or s.resolved or s.suppressed:
                print(
                    f"  {name:24} +{s.opened} new  {s.refreshed} open  -{s.resolved} resolved"
                    + (f"  {s.suppressed} suppressed" if s.suppressed else "")
                )
    return 0


def run_outliers_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        result = check_row_outliers(session, get_source(session, args.name))
        print(f"row outliers in {args.name}: {result.summary()}")
        for ref, error in result.errors.items():
            print(f"  could not check {ref}: {error}")
    return 0


def run_dbhealth_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        result = check_db_health(session, get_source(session, args.name))
        print(f"database health of {args.name}: {result.summary()}")
        for reading, why in result.skipped.items():
            print(f"  skipped {reading}: {why}")
    return 0


def run_findings_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        findings = open_findings(session, get_source(session, args.name))
        findings.sort(key=lambda f: (_SEVERITY_ORDER.index(f.severity), f.check_name))
        for f in findings:
            print(
                f"{str(f.id)[:8]}  {f.severity.value:8} {f.status.value:9} "
                f"{f.check_name:22} {f.title}"
            )
        print(f"{len(findings)} open findings  (review: aide finding confirm|reject|resolve ID)")
    return 0


def run_finding_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        finding = find_finding(session, args.id)
        if args.finding_command == "show":
            print(_describe(session, finding))
            return 0
        if args.finding_command == "explain":
            source = session.get_one(DataSource, finding.data_source_id)
            _explain(session, source, [finding])
            print(_explanation(finding))
            return 0
        status = _ACTIONS[args.finding_command]
        change_status(
            session,
            finding,
            status,
            actor=args.by or getpass.getuser(),
            now=utcnow(),
            note=args.note,
        )
        print(f"{status.value}: {finding.title}")
    return 0


def run_explain_command(args: argparse.Namespace) -> int:
    from ai_data_engineer.reasoning.explain import unexplained

    with store_session() as session:
        source = get_source(session, args.name)
        findings = unexplained(session, source, args.limit)
        if not findings:
            print("every open finding already has an up-to-date explanation")
            return 0
        _explain(session, source, findings)
        for f in findings:
            print(f"{str(f.id)[:8]}  {f.severity.value:8} {f.title}")
            print(_explanation(f) + "\n")
    return 0


def _explain(session: Session, source: DataSource, findings: list[Finding]) -> None:
    # Imported here: only these commands need an LLM.
    from ai_data_engineer.config import get_settings
    from ai_data_engineer.reasoning.explain import explain_findings
    from ai_data_engineer.reasoning.llm import llm_from_settings

    result = explain_findings(session, source, llm_from_settings(get_settings()), findings)
    print(result.summary())
    for problem in result.rejected:
        print(f"  rejected: {problem}")


def _explanation(f: Finding) -> str:
    e = f.evidence.get("explanation")
    if not e:
        return "  (no AI explanation)"
    confidence = f", confidence {e['confidence']:.2f}" if e.get("confidence") is not None else ""
    lines = [f"  AI explanation ({e.get('provider')}/{e.get('model')}{confidence}):"]
    if e.get("explained_title") != f.title:
        lines.append("  (written for an earlier version of this finding; numbers may differ)")
    lines.append(f"    {e['summary']}")
    if e.get("impact"):
        lines.append(f"    Impact: {e['impact']}")
    lines += [f"    Possible cause: {c}" for c in e.get("likely_causes", [])]
    lines += [f"    Check: {s}" for s in e.get("next_steps", [])]
    return "\n".join(lines)


def find_finding(session: Session, id_prefix: str) -> Finding:
    matches = [f for f in session.scalars(select(Finding)) if str(f.id).startswith(id_prefix)]
    if len(matches) != 1:
        raise LookupError(f"{len(matches)} findings match id prefix {id_prefix!r}")
    return matches[0]


def _describe(session: Session, f: Finding) -> str:
    events = session.scalars(
        select(FindingEvent).where(FindingEvent.finding_id == f.id).order_by(FindingEvent.at)
    ).all()
    evidence = {k: v for k, v in f.evidence.items() if k != "explanation"}
    lines = [
        f"{f.title}",
        f"  id:        {f.id}",
        f"  status:    {f.status.value}   severity: {f.severity.value}   check: {f.check_name}",
        f"  category:  {f.category.value}",
        f"  detected:  {f.first_detected_at.isoformat()}",
        f"  last seen: {f.last_detected_at.isoformat()}",
        "",
        f"  {f.description}",
        "",
        "  evidence:",
        *(f"    {line}" for line in json.dumps(evidence, indent=2, default=str).splitlines()),
        "",
        *([_explanation(f), ""] if "explanation" in f.evidence else []),
        "  history:",
    ]
    for e in events:
        change = f"{e.from_status.value if e.from_status else 'new'} -> {e.to_status.value}"
        note = f": {e.note}" if e.note else ""
        lines.append(f"    {e.at.isoformat()}  {e.actor:12} {change}{note}")
    return "\n".join(lines)
