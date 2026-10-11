"""``aide ask`` / ``aide questions`` / ``aide answer`` and ``aide context ...``."""

import argparse
import getpass
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from ai_data_engineer.context.dbt import import_dbt
from ai_data_engineer.context.questions import Question, answer, ask_all, generate
from ai_data_engineer.context.store import (
    CRITICALITY_LEVELS,
    WEEKDAYS,
    ContextError,
    current_notes,
    parse_criticality,
    parse_weekdays,
    set_note,
)
from ai_data_engineer.discovery.catalog import load_catalog
from ai_data_engineer.graph.models import AssetColumn, DataSource, utcnow
from ai_data_engineer.ingestion.cli import store_session
from ai_data_engineer.ingestion.sources import get_source


def add_context_parsers(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    p = subcommands.add_parser("ask", help="answer the engine's questions about a source")
    p.add_argument("name", help="data source name")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--by", help="who answers (default: OS user)")
    p = subcommands.add_parser("questions", help="list the open questions (with keys)")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=20)
    p = subcommands.add_parser("answer", help="answer one question by its key")
    p.add_argument("name")
    p.add_argument("key", help="question key from `aide questions`")
    p.add_argument("value", help="yes / no / a value")
    p.add_argument("--by")

    ctx = subcommands.add_parser("context", help="what people told the engine about a source")
    commands = ctx.add_subparsers(dest="context_command", required=True)
    p = commands.add_parser("show", help="everything recorded for a source")
    p.add_argument("name")
    p = commands.add_parser("describe", help="describe the source, a table or a column")
    p.add_argument("name")
    p.add_argument("target", help="'-' (the source), schema.table, or schema.table.column")
    p.add_argument("text")
    p.add_argument("--by")
    p = commands.add_parser("set", help="importance, freshness deadline, closed weekdays")
    p.add_argument("name")
    p.add_argument("target", help="schema.table (or '-' for --closed)")
    p.add_argument("--critical", help="1 low / 2 normal / 3 critical")
    p.add_argument("--deadline", type=float, help="hours without new rows before it's late")
    p.add_argument("--clock", help="the time column the deadline uses")
    p.add_argument("--closed", help="weekdays the business is closed, e.g. sat,sun or none")
    p.add_argument("--by")
    p = commands.add_parser("import-dbt", help="descriptions + tests from a dbt schema.yml")
    p.add_argument("name")
    p.add_argument("file", type=Path)
    p.add_argument("--schema", help="the database schema dbt builds into (default: any)")


def run_ask_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        source = get_source(session, args.name)
        questions = generate(session, source, now=utcnow(), limit=args.limit)
        if not questions:
            print("no open questions: everything useful has been answered")
            return 0
        print(f"{len(questions)} questions about {args.name}. Enter = the suggested answer, "
              "? = don't know.\n")  # fmt: skip

        def read(q: Question) -> str:
            suggestion = f" [{q.default}]" if q.default else ""
            return input(f"{q.prompt}\n  ({q.hint}){suggestion}: ")

        for _, outcome in ask_all(session, source, questions, read, by=_by(args), now=utcnow()):
            print(f"  -> {outcome}\n")
    return 0


def run_questions_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        source = get_source(session, args.name)
        questions = generate(session, source, now=utcnow(), limit=args.limit)
        for q in questions:
            print(f"{q.key}\n  {q.prompt}\n  ({q.hint}) suggested: {q.default or '-'}")
        print(f"{len(questions)} open questions (answer: aide answer {args.name} KEY VALUE)")
    return 0


def run_answer_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        source = get_source(session, args.name)
        found = {q.key: q for q in generate(session, source, now=utcnow(), limit=10_000)}
        if args.key not in found:
            raise ContextError(f"no open question {args.key!r} (see aide questions)")
        print(answer(session, source, found[args.key], args.value, by=_by(args), now=utcnow()))
    return 0


def run_context_command(args: argparse.Namespace) -> int:
    with store_session() as session:
        source = get_source(session, args.name)
        command = args.context_command
        if command == "show":
            _show(session, source)
        elif command == "describe":
            asset_key, column_key = _target(session, source, args.target)
            set_note(session, source, "description", {"text": args.text}, by=_by(args),
                     now=utcnow(), asset_key=asset_key, column_key=column_key)  # fmt: skip
            print(f"described {args.target}")
        elif command == "set":
            _set(session, source, args)
        else:
            result = import_dbt(session, source, args.file, by=_by(args), now=utcnow(),
                                schema=args.schema)  # fmt: skip
            print(result.summary())
            for line in result.rules_added:
                print(f"  rule: {line}")
            for line in result.links_not_found:
                print(f"  relationship not discovered yet: {line}")
            for line in result.skipped_tests:
                print(f"  test not used: {line}")
    return 0


def _set(session: Session, source: DataSource, args: argparse.Namespace) -> None:
    now, by = utcnow(), _by(args)
    if args.closed is not None:
        days = parse_weekdays(args.closed)
        set_note(session, source, "closed_weekdays", {"days": days}, by=by, now=now)
        print(f"closed weekdays: {', '.join(WEEKDAYS[d] for d in days) or 'none'}")
    if args.critical is None and args.deadline is None:
        return
    asset_key, column_key = _target(session, source, args.target)
    if asset_key is None or column_key is not None:
        raise ContextError("--critical and --deadline take a table: schema.table")
    if args.critical is not None:
        level = parse_criticality(args.critical)
        set_note(session, source, "criticality", {"level": level}, by=by, now=now,
                 asset_key=asset_key)  # fmt: skip
        print(f"{args.target}: {CRITICALITY_LEVELS[level]}")
    if args.deadline is not None:
        if not args.clock:
            raise ContextError("--deadline needs --clock (the time column to watch)")
        value = {"column": args.clock, "max_age_hours": args.deadline}
        set_note(session, source, "freshness", value, by=by, now=now, asset_key=asset_key)
        print(f"{args.target}: late after {args.deadline:g} h without new {args.clock}")


def _target(
    session: Session, source: DataSource, target: str
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    if target == "-":
        return None, None
    catalog = load_catalog(session, source)
    parts = target.split(".")
    table = catalog.tables.get(".".join(parts[:2]))
    if table is None:
        raise ContextError(f"no table {'.'.join(parts[:2])!r} in {source.name}")
    if len(parts) == 2:
        return table.asset_key, None
    column = table.columns.get(".".join(parts[2:]))
    if column is None:
        raise ContextError(f"no column {target!r}")
    return table.asset_key, column.column_key


def _show(session: Session, source: DataSource) -> None:
    catalog = load_catalog(session, source)
    tables = {t.asset_key: t.ref for t in catalog.tables.values()}
    notes = current_notes(session, source)
    for note in notes:
        where = tables.get(note.asset_key, "(source)") if note.asset_key else "(source)"
        if note.column_key:
            column = session.get(AssetColumn, note.column_key)
            where += f".{column.name}" if column else ""
        print(f"{where:45} {note.key:16} {note.value}  ({note.created_by})")
    print(f"{len(notes)} notes")


def _by(args: argparse.Namespace) -> str:
    return getattr(args, "by", None) or getpass.getuser()
