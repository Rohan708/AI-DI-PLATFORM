"""The Lumida API (Stage 4.2): what the web app (4.4) and a customer's own scripts use.

- **Off by default.** It starts only when ``LUMIDA_API_TOKEN`` is set; every request must
  send it as ``Authorization: Bearer <token>``. Accounts and roles replace the shared
  token in Stage 4.3.
- Runs inside ``lumida serve`` (one container), or alone with ``lumida api``.
- Interactive docs at ``/api/docs``, the machine-readable description at ``/api/openapi.json``.
"""

import logging
import secrets
import threading
from collections.abc import Callable
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from sqlalchemy import Engine

from lumida import __version__
from lumida.api.routes import router
from lumida.api.views import NotFoundError
from lumida.context.questions import AnswerError
from lumida.context.store import ContextError
from lumida.detection.recording import InvalidTransitionError
from lumida.ingestion.sources import SourceNotFoundError
from lumida.rules.store import InvalidRuleError

log = logging.getLogger("lumida.api")

# A request that can't be done as asked (the CLI prints these as one line): the API returns
# {"detail": "..."} with 404 or 409. Anything else is a bug and stays a 500.
NOT_FOUND = (SourceNotFoundError, NotFoundError)
CONFLICT = (InvalidTransitionError, InvalidRuleError, AnswerError, ContextError)

Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))]


def create_app(engine: Engine, token: str) -> FastAPI:
    if not token:
        raise ValueError("the API needs a token (LUMIDA_API_TOKEN)")

    def require_token(credentials: Credentials) -> None:
        sent = credentials.credentials if credentials else ""
        if not secrets.compare_digest(sent.encode(), token.encode()):
            raise HTTPException(401, "missing or wrong API token",
                                headers={"WWW-Authenticate": "Bearer"})  # fmt: skip

    app = FastAPI(
        title="Lumida API",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.engine = engine
    app.include_router(router, dependencies=[Depends(require_token)])

    for errors, code in ((NOT_FOUND, 404), (CONFLICT, 409)):
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
