"""``lumida serve`` (the nightly scheduler, plus the API when a token is set), ``lumida api``
(the API alone) and ``lumida migrate``."""

import argparse
import sys
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
    p = subcommands.add_parser("api", help="run only the API (needs LUMIDA_API_TOKEN)")
    p.add_argument("--host", help="default: LUMIDA_API_HOST (127.0.0.1)")
    p.add_argument("--port", type=int, help="default: LUMIDA_API_PORT (8000)")


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


def run_api_command(args: argparse.Namespace) -> int:
    from lumida.api.app import api_server, create_app
    from lumida.db import create_db_engine

    settings = get_settings()
    if settings.api_token is None:
        print("error: set LUMIDA_API_TOKEN first (a long random string; see .env.example)",
              file=sys.stderr)  # fmt: skip
        return 1
    configure_logging(settings)
    engine = create_db_engine(settings.database_url.get_secret_value())
    try:
        app = create_app(engine, settings.api_token.get_secret_value())
        api_server(app, args.host or settings.api_host, args.port or settings.api_port).run()
    finally:
        engine.dispose()
    return 0
