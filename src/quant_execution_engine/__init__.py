"""quant-execution-engine — canonical order router + sole broker-credential owner.

Standalone EXTERNAL engine (host :8400, container :8000) that the gateway proxies
under ``/api/v2/engines/execution/*``. See ``docs/plans/ROADMAP.md``.
"""

from __future__ import annotations

__version__ = "0.1.0"
