"""Logging setup.

Rule for the whole codebase: never log secrets or raw customer values — only
identifiers (database.schema.table.column) and aggregates.
"""

import logging

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format=_FORMAT)
