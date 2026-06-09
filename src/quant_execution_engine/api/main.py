"""Execution-engine FastAPI app.

Scaffold state: only ``GET /health`` is implemented. The order-routing surface
(``POST /orders``, ``GET /orders/{client_order_id}``, ``DELETE`` cancel, capability
matrix, order-update stream) is authored in Phase 2 of ``docs/plans/ROADMAP.md`` —
deliberately not implemented yet so no order can reach a broker from this scaffold.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from src.quant_execution_engine import __version__

logger = logging.getLogger(__name__)

app = FastAPI(
    title="quant-execution-engine",
    version=__version__,
    summary="Canonical order router + sole broker order-routing-credential owner.",
)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe. Mapped to host ``:8400`` (container ``:8000``) in compose."""
    return {"status": "ok", "service": "quant-execution-engine", "version": __version__}
