"""Minimal Engine.IO v4 / Socket.IO framing for the Liberator feed (Phase 5).

Only the slice the BidOfferV2 feed needs (ADR D19), no socket.io dependency:

* Engine.IO open packet ``0{json}`` (server → client on connect)
* Engine.IO ping ``2`` (server) → pong ``3`` (client)
* Socket.IO connect ``40`` (default namespace) and ``40/<NS>,`` (named)
* Socket.IO event ``42/<NS>,[...]`` for room join/leave and ``update`` data frames

Pure string helpers + a tiny frame classifier so the provider's recv loop and the
tests can both reason about raw frames without a live socket.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

# The broker requires these default namespaces joined BEFORE BidOfferV2 (a legacy
# finding, kept) — join them all, then BidOfferV2.
DEFAULT_NAMESPACES: tuple[str, ...] = (
    "MarketStatusV2",
    "TFEXDashboardV2",
    "MarketIndexV2",
    "StockV2",
    "TickerV2",
)
BID_OFFER_NS = "BidOfferV2"

_EIO_OPEN = "0"
_EIO_PING = "2"
_EIO_PONG = "3"
_SIO_CONNECT = "40"
_SIO_EVENT = "42"


class FrameKind(Enum):
    """The recv-loop's coarse classification of a raw frame."""

    OPEN = auto()
    PING = auto()
    EVENT = auto()
    OTHER = auto()


@dataclass(frozen=True)
class EventFrame:
    """A decoded ``42/<NS>,[name, arg]`` Socket.IO event frame."""

    namespace: str
    name: str
    arg: Any


def connect_default_packet() -> str:
    """The default-namespace connect packet (``40``)."""
    return _SIO_CONNECT


def connect_namespace_packet(namespace: str) -> str:
    """A named-namespace connect packet (``40/<NS>,``)."""
    return f"{_SIO_CONNECT}/{namespace},"


def pong_packet() -> str:
    """The Engine.IO pong reply to a server ping."""
    return _EIO_PONG


def join_rooms_packet(order_book_ids: list[int]) -> str:
    """Batch room-join event for BidOfferV2 (``42/BidOfferV2,["join","[id…]"]``)."""
    inner = "[" + ",".join(str(i) for i in order_book_ids) + "]"
    return f"{_SIO_EVENT}/{BID_OFFER_NS}," + json.dumps(["join", inner])


def leave_room_packet(order_book_id: int) -> str:
    """Room-leave event for one id (``42/BidOfferV2,["leave","[id]"]``)."""
    inner = f"[{order_book_id}]"
    return f"{_SIO_EVENT}/{BID_OFFER_NS}," + json.dumps(["leave", inner])


def classify(raw: str) -> FrameKind:
    """Coarsely classify a raw frame for the recv loop."""
    if raw == _EIO_PING:
        return FrameKind.PING
    if raw.startswith(_EIO_OPEN) and not raw.startswith(_SIO_CONNECT):
        return FrameKind.OPEN
    if raw.startswith(_SIO_EVENT):
        return FrameKind.EVENT
    return FrameKind.OTHER


def decode_event(raw: str) -> EventFrame | None:
    """Decode a ``42[/<NS>],[name, arg]`` frame; ``None`` if not a usable event.

    Namespace defaults to ``"/"`` when absent. Returns ``None`` for malformed
    JSON, a non-list body, or a body without at least a name element.
    """
    if not raw.startswith(_SIO_EVENT):
        return None
    body = raw[len(_SIO_EVENT) :]
    namespace = "/"
    if body.startswith("/"):
        comma = body.find(",")
        if comma == -1:
            return None
        namespace = body[1:comma]
        body = body[comma + 1 :]
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, list) or not decoded:
        return None
    name = decoded[0]
    arg = decoded[1] if len(decoded) > 1 else None
    if not isinstance(name, str):
        return None
    return EventFrame(namespace=namespace, name=name, arg=arg)
