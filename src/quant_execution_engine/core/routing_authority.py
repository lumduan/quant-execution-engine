"""EH6 — exactly one authoritative real-router per broker account.

Capture is safely active/active because frames are ``vs``-idempotent. **Orders are not.**
Two nodes routing one account is double fills, which is a money bug, not a data bug.

WHY THIS EXISTS RATHER THAN A MEASUREMENT
-----------------------------------------
The invariant has held since 2026-06-10 — but **by circumstance, not by mechanism**. Its
recorded verification was ``select broker, count(*)`` → ``sim|1``, and that check is weaker
than it looks: ``orders.broker`` records the **requested** broker, not the one contacted, so
a row reading ``liberator`` on a node with no Liberator credential is normal (the store keeps
the request; execution was intercepted to sim). The query cannot distinguish *routed real*
from *requested real, intercepted*. ⇒ it can never prove the invariant, only fail to disprove
it.

WHAT THIS CANNOT DO, STATED PLAINLY
-----------------------------------
There is **no cross-node coordination surface** on this platform: the ``db_execution`` stores
are deliberately node-local and never unioned, there is no node column, and no shared table in
which "account X is claimed by node Y" could be a UNIQUE constraint. So this guard **cannot
observe the other node.** It can only enforce what *this* node has been declared permitted to
route, and refuse everything else.

That makes the declaration mechanism the whole of its strength, hence:

* **absent ⇒ refuse.** Never absent ⇒ allow. A node that was never told what it may route may
  route nothing real.
* **the declaration is cross-checked against the credential the node actually holds** and a
  disagreement is refused rather than resolved. ⚠️ This is not belt-and-braces — it is the
  ADR's own lesson: EH6's invariant rested on EH7's account map, that map was **wrong for 31
  days**, and a guard keyed to it *"would have stood down the wrong node's account"*. A node
  cannot be wrong about which credentials it holds; it can very easily be wrong about a table.

Modelled on the kill-switch, which is enforcing rather than advisory for one reason worth
copying: **it has no enable flag.** Its "off" is a *value*, not a *bypass*. Contrast the price
band, which is gated away by ``enabled`` and is therefore satisfiable by circumstance.
"""

from __future__ import annotations

from typing import ClassVar

from src.quant_execution_engine.adapters.base import BrokerAdapter
from src.quant_execution_engine.config.errors import ConfigError
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.enums import Broker, Stage
from src.quant_execution_engine.contracts.errors import OrderRejectedError

_REAL_BROKERS = (Broker.LIBERATOR, Broker.STREAMING_PRO)
_REAL_ROUTING_STAGES = (Stage.MICRO_LIVE, Stage.LIVE)


class RealRoutingNotAuthorized(OrderRejectedError):
    """This node is not the authoritative real-router for that account (EH6).

    Maps to ``409 CONFLICT``: the request is well-formed and the venue would accept it — it is
    the *deployment* that forbids it. Distinct from ``stage_rejected`` (the ladder forbids the
    route at all) so the two cannot be confused in a log.
    """

    code: ClassVar[str] = "real_routing_not_authorized"


def authorized_accounts(settings: Settings) -> frozenset[str]:
    """The accounts this node may route REAL orders for. Empty means none."""
    return frozenset(a.strip() for a in settings.real_routing_accounts if a.strip())


def assert_may_route_real(
    *,
    settings: Settings,
    account: str,
    broker: Broker,
    adapter: BrokerAdapter,
    sim_adapter: BrokerAdapter,
) -> None:
    """Refuse unless this node is declared the real router for ``account``.

    🔑 **Binds on the RESOLVED ADAPTER being real, not on the stage.** That is exactly the set
    of cases where a venue would be contacted — at ``paper`` a placement is intercepted to
    ``SimAdapter`` and reaches nothing, so gating on stage would be both too broad (blocking
    harmless paper traffic) and too narrow (a future stage would slip past a hard-coded list).
    """
    if adapter is sim_adapter or broker not in _REAL_BROKERS:
        return  # nothing real is being contacted

    allowed = authorized_accounts(settings)
    if not allowed:
        raise RealRoutingNotAuthorized(
            f"this node is not declared the real router for any account, so it cannot route "
            f"{broker.value} order for {account!r} — set EXECUTION_ENGINE_REAL_ROUTING_ACCOUNTS "
            "(EH6: exactly one authoritative real-router per broker account)",
            detail={"account": account, "broker": broker.value, "declared": []},
        )
    if account not in allowed:
        raise RealRoutingNotAuthorized(
            f"account {account!r} is not in this node's real-routing declaration "
            f"(EH6). Declared: {sorted(allowed)}",
            detail={"account": account, "broker": broker.value, "declared": sorted(allowed)},
        )


def assert_startup_declaration(settings: Settings) -> None:
    """Refuse to START a deployment that would route real orders without a declaration (EH6).

    The submit-path guard would refuse every order anyway; refusing at start-up is the
    difference between noticing at boot and noticing on the first real order — and
    ``AWS_STANDUP.md`` records exactly why that matters: *"a defect that can only fire at
    micro_live is one that soaking at sim cannot reach."*

    🔑 **The credential cross-check is the load-bearing half.** A declaration is only
    trustworthy if it agrees with the credentials this node actually holds. EH6's invariant
    once rested on EH7's account map, that map was **wrong for 31 days**, and a guard keyed to
    it would have stood the wrong node down. A node cannot be mistaken about which credentials
    it was given; it can very easily be mistaken about a table. So a declaration with no broker
    credential behind it is a **contradiction that is refused, not resolved** — resolving it by
    picking a winner is how the 31 days happened.

    ⚠️ Deliberately NOT a ``Settings`` validator. That would refuse to construct a *data
    object*, breaking every unrelated test that builds a ``micro_live`` Settings for other
    reasons — and "this deployment must not start" is an app concern, not a model concern.
    """
    if settings.stage not in _REAL_ROUTING_STAGES:
        return
    declared = sorted(authorized_accounts(settings))
    if not declared:
        raise ConfigError(
            f"stage={settings.stage.value} requires EXECUTION_ENGINE_REAL_ROUTING_ACCOUNTS "
            "(EH6: exactly one authoritative real-router per broker account). Refusing to "
            "start rather than refusing every order at submit time."
        )
    if settings.liberator_api_key is None and settings.streaming_pro_api_key is None:
        raise ConfigError(
            f"EXECUTION_ENGINE_REAL_ROUTING_ACCOUNTS declares {declared} but this node holds "
            "NO broker credential — the declaration and the credentials disagree. Refusing "
            "rather than picking one (EH6/EH7: a stale account map went unnoticed for 31 days)."
        )
