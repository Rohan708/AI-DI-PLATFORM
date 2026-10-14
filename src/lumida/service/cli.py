"""``lumida serve`` (the nightly scheduler) and ``lumida migrate``."""

import argparse
import threading

from lumida.config import get_settings
from lumida.service.migrate import upgrade_to_head
from lumida.service.serve import configure_logging, install_signal_handlers, serve


def add_service_parsers(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    p = subcommands.add_parser(
        "serve", help="run every source nightly at its scheduled time (the self-hosted service)"
    )
    p.add_argument("--once", action="store_true", help="run whatever is due now, then exit")
    p.add_argument("--no-migrate", action="store_true", help="don't upgrade the store on start")
    subcommands.add_parser("migrate", help="bring the metadata store up to date")


def run_serve_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings)
    stop = threading.Event()
    install_signal_handlers(stop)
    serve(settings, once=args.once, migrate=not args.no_migrate, stop=stop)
    return 0


def run_migrate_command(args: argparse.Namespace) -> int:
    revision = upgrade_to_head(get_settings().database_url.get_secret_value())
    print(f"metadata store at migration {revision}")
    return 0
