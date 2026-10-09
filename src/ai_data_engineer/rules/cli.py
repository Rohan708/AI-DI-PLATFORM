"""``aide rules ...`` (per source) and ``aide rule ...`` (one rule) commands."""

import argparse
import getpass
import json

from ai_data_engineer.config import get_settings
from ai_data_engineer.discovery.catalog import load_catalog
from ai_data_engineer.graph.models import Rule, RuleStatus, utcnow
from ai_data_engineer.ingestion.cli import store_session
from ai_data_engineer.ingestion.sources import get_source
from ai_data_engineer.rules.checks import check_rules
from ai_data_engineer.rules.spec import parse_spec
from ai_data_engineer.rules.store import add_user_rule, find_rule, review_rule, rules_of

_ACTIONS = {
    "approve": RuleStatus.ACTIVE,
    "reject": RuleStatus.REJECTED,
    "disable": RuleStatus.DISABLED,
    "enable": RuleStatus.ACTIVE,
    "reconsider": RuleStatus.PROPOSED,
}


def add_rule_parsers(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    rules = subcommands.add_parser("rules", help="business rules of a source")
    commands = rules.add_subparsers(dest="rules_command", required=True)
    p = commands.add_parser("propose", help="ask the AI to propose rules (needs AIDE_LLM_*)")
    p.add_argument("name", help="data source name")
    p = commands.add_parser("list", help="rules with their status and id")
    p.add_argument("name")
    p.add_argument("--status", choices=[s.value for s in RuleStatus])
    p = commands.add_parser("check", help="run the active rules now")
    p.add_argument("name")
    p = commands.add_parser("add", help="add your own rule (JSON, see docs/design/ai_rules.md)")
    p.add_argument("name")
    p.add_argument("rule_json", help='e.g. {"kind": "compare_constant", "table": ...}')
    p.add_argument("--by", help="author (default: OS user)")

    rule = subcommands.add_parser("rule", help="show or review one rule")
    commands = rule.add_subparsers(dest="rule_command", required=True)
    p = commands.add_parser("show", help="definition, rationale and review history")
    p.add_argument("id", help="rule id (or a unique prefix)")
    for action, status in _ACTIONS.items():
        p = commands.add_parser(action, help=f"make one or more rules {status.value}")
        p.add_argument("ids", nargs="+", metavar="id", help="rule ids (or unique prefixes)")
        p.add_argument("--note", help="why (kept in the rule's history)")
        p.add_argument("--by", help="reviewer name (default: OS user)")


def run_rules_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        source = get_source(session, args.name)
        if args.rules_command == "propose":
            # Imported here: only this command needs an LLM.
            from ai_data_engineer.reasoning.llm import llm_from_settings
            from ai_data_engineer.reasoning.propose import propose_rules

            result = propose_rules(session, source, llm_from_settings(get_settings()))
            print(result.summary())
            for r in result.proposed:
                print(f"  {_line(r)}")
            for problem in result.invalid:
                print(f"  invalid: {problem}")
            if result.proposed:
                print("review: aide rule show ID, then aide rule approve|reject ID")
        elif args.rules_command == "list":
            status = RuleStatus(args.status) if args.status else None
            found = rules_of(session, source, status)
            for r in found:
                print(_line(r))
            print(f"{len(found)} rules")
        elif args.rules_command == "check":
            print(f"rules on {args.name}: {check_rules(session, source).summary()}")
        else:  # add
            rule = add_user_rule(
                session, source, load_catalog(session, source),
                parse_spec(json.loads(args.rule_json)), args.by or getpass.getuser(), utcnow(),
            )  # fmt: skip
            print(f"added (active): {_line(rule)}")
    return 0


def run_rule_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        if args.rule_command == "show":
            print(_describe(find_rule(session, args.id)))
            return 0
        # Look every id up first, so a typo changes nothing.
        rules = [find_rule(session, rule_id) for rule_id in args.ids]
        for rule in rules:
            review_rule(
                session,
                rule,
                _ACTIONS[args.rule_command],
                actor=args.by or getpass.getuser(),
                now=utcnow(),
                note=args.note,
            )
            print(f"{str(rule.id)[:8]} is now {rule.status.value}: {rule.name}")
    return 0


def _line(r: Rule) -> str:
    confidence = f"{r.confidence:.2f}" if r.confidence is not None else "  - "
    return f"{str(r.id)[:8]}  {r.status.value:8} {r.origin.value:6} {confidence}  {r.name}"


def _describe(r: Rule) -> str:
    lines = [
        r.name,
        f"  id:         {r.id}",
        f"  status:     {r.status.value}   origin: {r.origin.value}   by: {r.created_by}",
        f"  confidence: {r.confidence if r.confidence is not None else '-'}",
    ]
    if r.evidence.get("rationale"):
        lines.append(f"  rationale:  {r.evidence['rationale']}")
    lines += ["", "  definition:"]
    lines += [f"    {line}" for line in json.dumps(r.definition["spec"], indent=2).splitlines()]
    history = r.evidence.get("review_history", [])
    if history:
        lines += ["", "  history:"]
        lines += [
            f"    {h['at']}  {h['by']}: {h['from']} -> {h['to']}"
            + (f"  ({h['note']})" if h.get("note") else "")
            for h in history
        ]
    return "\n".join(lines)
