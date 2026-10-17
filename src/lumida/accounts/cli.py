"""``lumida user ...``: people who can log in to the API (and the web app, 4.4).

The first admin is created here, inside the container:
``docker compose exec -it lumida lumida user add you@company.com --role admin``.
Passwords are typed at a hidden prompt (or piped with ``--password-stdin``), never
passed as arguments, so they don't end up in shell history or process lists.
"""

import argparse
import getpass
import sys

from lumida.accounts.passwords import check_password_rules
from lumida.accounts.store import (
    AccountError,
    create_user,
    find_user,
    list_users,
    set_disabled,
    set_password,
    set_role,
)
from lumida.graph.models import Role
from lumida.ingestion.cli import store_session
from lumida.ingestion.sources import default_tenant

ROLES = [r.value for r in Role]


def add_user_parsers(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    user = subcommands.add_parser("user", help="people who can log in (API, web app)")
    commands = user.add_subparsers(dest="user_command", required=True)

    p = commands.add_parser("add", help="create an account")
    p.add_argument("email")
    p.add_argument("--role", choices=ROLES, default="viewer")
    p.add_argument("--name", default="", help="display name (default: the email)")
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")

    commands.add_parser("list", help="every account, with role and last login")

    p = commands.add_parser("role", help="change what someone may do")
    p.add_argument("email")
    p.add_argument("role", choices=ROLES)

    for name, text in (("disable", "stop someone logging in (ends their sessions and keys)"),
                       ("enable", "let a disabled person log in again")):  # fmt: skip
        p = commands.add_parser(name, help=text)
        p.add_argument("email")

    p = commands.add_parser("password", help="set a new password (also unlocks the account)")
    p.add_argument("email")
    p.add_argument("--password-stdin", action="store_true")


def run_user_command(args: argparse.Namespace) -> int:
    by = f"cli:{getpass.getuser()}"
    with store_session() as session:
        tenant = default_tenant(session)
        if args.user_command == "add":
            user = create_user(
                session, tenant_id=tenant.id, email=args.email, name=args.name,
                role=Role(args.role), password=_read_password(args.password_stdin), by=by,
            )  # fmt: skip
            print(f"created {user.email} ({user.role.value})")
        elif args.user_command == "list":
            for u in list_users(session, tenant.id):
                state = "disabled" if u.disabled_at else ("locked" if u.locked_until else "")
                last = u.last_login_at.isoformat(timespec="minutes") if u.last_login_at else "never"
                print(f"{u.email:32} {u.role.value:9} last login {last:16} {state}")
        else:
            user = find_user(session, args.email, tenant.id)
            if args.user_command == "role":
                set_role(session, user, Role(args.role), by=by)
                print(f"{user.email} is now {user.role.value}")
            elif args.user_command in ("disable", "enable"):
                set_disabled(session, user, args.user_command == "disable", by=by)
                print(f"{user.email} {args.user_command}d")
            else:
                set_password(session, user, _read_password(args.password_stdin), by=by)
                print(f"new password set for {user.email}; their sessions were ended")
    return 0


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        check_password_rules(password)
        return password
    password = getpass.getpass("password: ")
    check_password_rules(password)
    if getpass.getpass("again: ") != password:
        raise AccountError("the two passwords differ")
    return password
