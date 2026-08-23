"""Map the typed rejection taxonomy onto HTTP + the uniform error envelope.

Envelope: ``{"error": {"code", "message", "client_order_id?", "detail?"}}``.
Pydantic request-validation failures are wrapped into the same envelope
(``code = "validation_error"``) so consumers parse exactly one error shape.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.quant_execution_engine.contracts.errors import OrderRejectedError, RiskRejected

_STATUS_BY_CODE: dict[str, int] = {
    "public_mode": status.HTTP_403_FORBIDDEN,
    "kill_switch_engaged": status.HTTP_503_SERVICE_UNAVAILABLE,
    "kill_switch_env_pinned": status.HTTP_409_CONFLICT,
    "kill_switch_not_engaged": status.HTTP_409_CONFLICT,
    "stage_rejected": status.HTTP_403_FORBIDDEN,
    "capability_unsupported": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "risk_rejected": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "price_band_exceeded": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "duplicate_burst_detected": status.HTTP_409_CONFLICT,
    "order_not_found": status.HTTP_404_NOT_FOUND,
    "order_book_unavailable": status.HTTP_404_NOT_FOUND,
    "order_stream_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "illegal_transition": status.HTTP_409_CONFLICT,
    # 23514 on INSERT: the row does not satisfy a column CHECK. 422 (not 500) because it is
    # well-formed but refused, and TERMINAL — retrying it against the same schema cannot help.
    "store_constraint_violated": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "submit_in_flight": status.HTTP_409_CONFLICT,
    "amend_rejected": status.HTTP_409_CONFLICT,
    "broker_circuit_open": status.HTTP_503_SERVICE_UNAVAILABLE,
}

# The per-second rate cap is the throttle that maps to 429. The duplicate-burst
# guard now raises its own typed DuplicateBurstDetected (409, Phase 6 / A3), so
# it is no longer a RiskRejected cap here.
_THROTTLE_CAPS = frozenset({"rate_limit"})


def _status_for(exc: OrderRejectedError) -> int:
    if isinstance(exc, RiskRejected) and exc.cap in _THROTTLE_CAPS:
        return status.HTTP_429_TOO_MANY_REQUESTS
    return _STATUS_BY_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST)


def register_error_handlers(app: FastAPI) -> None:
    """Install the envelope handlers on the app."""

    @app.exception_handler(OrderRejectedError)
    async def _handle_rejection(request: Request, exc: OrderRejectedError) -> JSONResponse:
        body: dict[str, object] = {"code": exc.code, "message": exc.message}
        if exc.client_order_id is not None:
            body["client_order_id"] = exc.client_order_id
        if exc.detail:
            body["detail"] = exc.detail
        return JSONResponse(status_code=_status_for(exc), content={"error": body})

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        compact = [
            {"loc": list(map(str, e.get("loc", ()))), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "request validation failed",
                    "detail": {"errors": compact},
                }
            },
        )
