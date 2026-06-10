"""Root exception for the execution engine.

Every subpackage defines module-local exceptions in its own ``errors.py``,
all inheriting (directly or indirectly) from :class:`ExecutionEngineError`.
"""

from __future__ import annotations


class ExecutionEngineError(Exception):
    """Base class for every error raised by this service."""
