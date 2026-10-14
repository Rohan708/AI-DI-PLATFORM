"""Command-line entry point: ``lumida``.

Everyday use is ``lumida run <source>`` (the whole nightly job). The individual steps are
also available: ``source``/``scan`` (1.3), ``discover``/``relationships``/``docs`` (1.4),
``detect``/``findings``/``finding`` (1.5-1.6), ``health`` (1.6), ``rules``/``rule``
(Stage 2: AI-proposed, human-approved business rules), and ``lab`` (test lab).
"""

import argparse
import json
import sys
from collections.abc import Callable, Sequence

from pydantic import ValidationError

from lumida import __version__
from lumida.context.cli import (
    add_context_parsers,
    run_answer_command,
    run_ask_command,
    run_context_command,
    run_questions_command,
)
from lumida.context.questions import AnswerError
from lumida.context.store import ContextError
from lumida.detection.cli import (
    add_detection_parsers,
    run_dbhealth_command,
    run_detect_command,
    run_explain_command,
    run_finding_command,
    run_findings_command,
    run_outliers_command,
)
from lumida.detection.recording import InvalidTransitionError
from lumida.discovery.cli import (
    add_discovery_parsers,
    run_discover_command,
    run_docs_command,
    run_relationships_command,
)
from lumida.ingestion.cli import (
    add_ingestion_parsers,
    run_scan_command,
    run_source_command,
)
from lumida.ingestion.connections import ConnectionRefError
from lumida.ingestion.scan import UnsupportedSourceError
from lumida.ingestion.sources import SourceExistsError, SourceNotFoundError
from lumida.lab.cli import add_lab_parser, run_lab_command
from lumida.lab.runner import UnknownScenarioError
from lumida.lab.schema import LabSafetyError
from lumida.lab.state import LabNotBuiltError
from lumida.pipeline_cli import (
    add_pipeline_parsers,
    run_health_command,
    run_run_command,
)
from lumida.reasoning.llm import LLMError
from lumida.reconcile.cli import add_reconcile_parser, run_reconcile_command
from lumida.reconcile.run import PairConfigError, PairNotFoundError
from lumida.rules.cli import add_rule_parsers, run_rule_command, run_rules_command
from lumida.rules.store import InvalidRuleError
from lumida.semantic.cli import add_semantic_parser, run_pii_command

# Mistakes a user can make: reported as one line, not a traceback.
USER_ERRORS = (
    SourceExistsError,
    SourceNotFoundError,
    ConnectionRefError,
    UnsupportedSourceError,
    LabNotBuiltError,
    LabSafetyError,
    UnknownScenarioError,
    InvalidTransitionError,
    InvalidRuleError,
    LLMError,
    PairNotFoundError,
    PairConfigError,
    ContextError,
    AnswerError,
    json.JSONDecodeError,
    ValidationError,
    LookupError,
)

_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "run": run_run_command,
    "health": run_health_command,
    "source": run_source_command,
    "scan": run_scan_command,
    "discover": run_discover_command,
    "relationships": run_relationships_command,
    "docs": run_docs_command,
    "detect": run_detect_command,
    "findings": run_findings_command,
    "finding": run_finding_command,
    "explain": run_explain_command,
    "outliers": run_outliers_command,
    "dbhealth": run_dbhealth_command,
    "rules": run_rules_command,
    "rule": run_rule_command,
    "reconcile": run_reconcile_command,
    "pii": run_pii_command,
    "ask": run_ask_command,
    "questions": run_questions_command,
    "answer": run_answer_command,
    "context": run_context_command,
    "lab": run_lab_command,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumida", description="Lumida CLI")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("version", help="print the installed version")
    add_pipeline_parsers(subcommands)
    add_ingestion_parsers(subcommands)
    add_discovery_parsers(subcommands)
    add_detection_parsers(subcommands)
    add_rule_parsers(subcommands)
    add_reconcile_parser(subcommands)
    add_semantic_parser(subcommands)
    add_context_parsers(subcommands)
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
        return 0
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
