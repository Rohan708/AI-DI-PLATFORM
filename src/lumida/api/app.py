"""The Lumida API: what the web app (4.4) and a customer's own scripts use.

- **Personal logins** (Stage 4.3): ``POST /api/v1/auth/login`` with email + password
  returns a token; every other request sends it as ``Authorization: Bearer <token>``.
  Scripts use personal API keys (``/api/v1/auth/keys``). Roles: viewer, reviewer, admin.
- Runs inside ``lumida serve`` (one container), or alone with ``lumida api``.
- Interactive docs at ``/api/docs``, the machine-readable description at ``/api/openapi.json``.
"""

import logging
import threading
from collections.abc import Callable

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import Engine

from lumida import __version__
from lumida.accounts.passwords import WeakPasswordError
from lumida.accounts.store import AccountError, AccountNotFoundError, AuthPolicy
from lumida.api import account_routes, routes
from lumida.api.views import NotFoundError
from lumida.context.questions import AnswerError
from lumida.context.store import ContextError
from lumida.detection.recording import InvalidTransitionError
from lumida.ingestion.sources import SourceNotFoundError
from lumida.rules.store import InvalidRuleError

log = logging.getLogger("lumida.api")

# A request that can't be done as asked (the CLI prints these as one line): the API returns
# {"detail": "..."} with 404, 409 or 422. Anything else is a bug and stays a 500.
NOT_FOUND = (SourceNotFoundError, NotFoundError, AccountNotFoundError)
CONFLICT = (InvalidTransitionError, InvalidRuleError, AnswerError, ContextError, AccountError)
INVALID = (WeakPasswordError,)


def create_app(engine: Engine, policy: AuthPolicy | None = None) -> FastAPI:
    app = FastAPI(
        title="Lumida API",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.engine = engine
    app.state.policy = policy or AuthPolicy()
    app.include_router(account_routes.router)
    app.include_router(routes.router)

    for errors, code in ((NOT_FOUND, 404), (CONFLICT, 409), (INVALID, 422)):
        for error in errors:
            app.add_exception_handler(error, _reply(code))

    @app.exception_handler(ValidationError)
    def invalid(_request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse({"detail": _validation(exc)}, status_code=422)  # e.g. a rule spec

    return app


def _reply(code: int) -> Callable[[Request, Exception], JSONResponse]:
    def handle(_request: Request, exc: Exception) -> JSONResponse:
        detail = str(exc.args[0]) if exc.args else type(exc).__name__
        return JSONResponse({"detail": detail}, status_code=code)

    return handle


def _validation(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" if e["loc"] else e["msg"]
        for e in exc.errors(include_url=False, include_input=False)
    )


def api_server(app: FastAPI, host: str, port: int) -> uvicorn.Server:
    """A uvicorn server that logs through our own logging setup (JSON in the image)."""
    config = uvicorn.Config(app, host=host, port=port, log_config=None, access_log=True)
    return uvicorn.Server(config)


def start_api_thread(server: uvicorn.Server) -> threading.Thread:
    """Run the API next to the scheduler. Uvicorn leaves signal handling to the main
    thread when it isn't in it, so ``docker stop`` still reaches the scheduler."""
    thread = threading.Thread(target=server.run, name="lumida-api", daemon=True)
    thread.start()
    return thread
