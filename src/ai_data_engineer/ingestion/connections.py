"""Resolve a data source's ``connection_ref`` to a connection URL.

The metadata store never holds credentials: ``connection_ref`` is the *name* of an
environment variable (read from the process environment, then from ``.env``) whose
value is the connection URL. A secrets-manager backend can be added behind this
function later without touching callers.
"""

import os
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_ENV_FILE = Path(".env")


class ConnectionRefError(LookupError):
    pass


def resolve_connection_url(connection_ref: str | None, *, env_file: Path = DEFAULT_ENV_FILE) -> str:
    if not connection_ref:
        raise ConnectionRefError("data source has no connection_ref")
    value = os.environ.get(connection_ref)
    if not value and env_file.exists():
        value = dotenv_values(env_file).get(connection_ref)
    if not value:
        raise ConnectionRefError(
            f"environment variable {connection_ref!r} is not set (process env or {env_file})"
        )
    return value
