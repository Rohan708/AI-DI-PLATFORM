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
_PREFIXES = ("LUMIDA_", "AIDE_")  # current and pre-rename (2026-10-14) variable prefixes


def _renamed(name: str) -> list[str]:
    for prefix in _PREFIXES:
        if name.startswith(prefix):
            rest = name[len(prefix) :]
            return [other + rest for other in _PREFIXES if other != prefix]
    return []


class ConnectionRefError(LookupError):
    pass


def resolve_connection_url(connection_ref: str | None, *, env_file: Path = DEFAULT_ENV_FILE) -> str:
    if not connection_ref:
        raise ConnectionRefError("data source has no connection_ref")
    from_file = dotenv_values(env_file) if env_file.exists() else {}
    value: str | None = None
    # The same name under the product's old prefix also counts (AIDE_X <-> LUMIDA_X), so
    # sources registered before the rename and .env files written before it keep working.
    for name in (connection_ref, *_renamed(connection_ref)):
        value = os.environ.get(name) or from_file.get(name)
        if value:
            break
    if not value:
        raise ConnectionRefError(
            f"environment variable {connection_ref!r} is not set (process env or {env_file})"
        )
    return value
