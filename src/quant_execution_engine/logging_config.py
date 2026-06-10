"""Logging configuration for the execution engine."""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(level: str) -> None:
    """Configure root logging once at startup.

    Never logs secrets: callers must not pass credentials or full account
    references into log records (hard rule).
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=_FORMAT,
    )
