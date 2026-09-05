"""GET /accounts/{account} and /accounts/{account}/open-orders — the broker-read routes.

These exist so a strategy never has to know WHICH broker it is talking to (GH #234).
The load-bearing property is not that they return data — it is that **`None` survives as
`null`**. A broker-unreported field rendered as `0` is [[TK-0396]] exactly: a fabricated
zero was returned for accounts holding real five-figure balances, and a confident zero is
the shape that passes a smoke test.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.quant_execution_engine.adapters.base import (
    AccountInfo,
    AccountType,
    AmendAck,
    BrokerAdapter,
    CancelAck,
    PlaceAck,
    Position,
)
from src.quant_execution_engine.adapters.liberator.errors import (
    LiberatorAccountNotFound,
)
from src.quant_execution_engine.api.main import create_app
from src.quant_execution_engine.contracts.capabilities import CapabilitySet, lookup
from src.quant_execution_engine.contracts.enums import Broker, Market, Side
from src.quant_execution_engine.contracts.orders import NormalizedOrder

from tests._fakes import FakeRedis, MemStore, patch_repositories
from tests.conftest import make_order, make_settings

# session:cash-carry's verification control, read live from the bridge 2026-08-27.
# They supplied these precisely so "is it right?" is a one-command check rather than a
# judgement — a 0 for either is the OLD FAILURE, not a flat account.
_CONTROL_CASH = Decimal("50000.11")  # 70000002, CASH BALANCE
_CONTROL_DERIV = Decimal("13000.22")  # 70000007, DERIVATIVE
_ACCOUNT = "70000002"


class _ReadAdapter(BrokerAdapter):
    """A broker whose account genuinely reports only SOME fields."""

    def __init__(self, *, info: AccountInfo | None = None, raises: Exception | None = None) -> None:
        super().__init__()
        self.broker = Broker.LIBERATOR  # type: ignore[misc]
        self._info = info
        self._raises = raises
        self.open_orders: list[NormalizedOrder] = []
        self.positions: list[Position] = []
        self.positions_raise: Exception | None = None

    async def place(self, order: NormalizedOrder) -> PlaceAck:
        return PlaceAck(broker_order_id="X-1")

    async def cancel(self, client_order_id: str) -> CancelAck:
        return CancelAck(ok=True)

    async def amend(
        self, client_order_id: str, new_price: Decimal | None = None, new_qty: int | None = None
    ) -> AmendAck:
        return AmendAck(ok=True, semantics="cancel_replace")

    async def get_open_orders(self, account: str) -> list[NormalizedOrder]:
        if self._raises is not None:
            raise self._raises
        return self.open_orders

    async def get_positions(self, account: str) -> list[Position]:
        """Configurable now that the route exists.

        ⚠️ Was an unconditional ``raise LiberatorPositionsUncaptured`` — correct while
        `get_positions` was unimplemented, and it silently made every positions-route
        test 501 the moment one was written. Kept raising BY DEFAULT only when a test
        asks for it, so the refusal path is still exercised deliberately rather than
        by accident.
        """
        if self.positions_raise is not None:
            raise self.positions_raise
        return self.positions

    async def get_account(self, account: str) -> AccountInfo:
        if self._raises is not None:
            raise self._raises
        assert self._info is not None
        return self._info

    def capabilities(self) -> tuple[CapabilitySet, ...]:
        return (lookup(self.broker, Market.SET),)

    async def heartbeat(self) -> bool:
        return True


def _client(monkeypatch: pytest.MonkeyPatch, adapter: _ReadAdapter, **overrides: Any) -> TestClient:
    """micro_live + owner mode + both accounts declared, constructed DIRECTLY.

    micro_live is built here rather than soaked into: a route that only routes to a
    real adapter at micro_live is one that testing at sim cannot reach. The router is
    injected through the dependency override so the test drives the REAL production
    path (`OrderRouter._resolve_adapter` -> stage ladder -> EH6), not a stand-in.
    """
    from src.quant_execution_engine.api import deps
    from src.quant_execution_engine.core.router import OrderRouter

    store = MemStore()
    patch_repositories(monkeypatch, store)
    base: dict[str, Any] = {
        "public_mode": False,
        "stage": "micro_live",
        "real_routing_accounts": [_ACCOUNT, "70000007"],
    }
    settings = make_settings(**{**base, **overrides})  # overrides win
    order_router = OrderRouter(
        settings=settings, pool=object(), redis=FakeRedis(), liberator_adapter=adapter
    )
    app = create_app()
    app.dependency_overrides[deps.get_settings_dep] = lambda: settings
    app.dependency_overrides[deps.get_pool_dep] = lambda: object()
    app.dependency_overrides[deps.get_redis_dep] = lambda: FakeRedis()
    app.dependency_overrides[deps.get_router_dep] = lambda: order_router
    # This file constructs the client DIRECTLY rather than via `build_client`, so it must
    # supply the api-key itself — the guard fails closed without one ([[TK-0462]]) and a
    # configured app rejects an unauthenticated request with 401.
    headers = {"X-API-Key": settings.api_key} if settings.api_key else {}
    return TestClient(app, headers=headers)


# ------------------------------------------------------------------ the contract


def test_a_field_the_broker_DID_NOT_REPORT_serialises_as_null_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 THE TK-0396 REGRESSION GUARD, asserted on the wire.

    A cash account reports no equity and no margin. Those fields must come back as
    JSON ``null``. If any of them renders as ``0``, a caller cannot distinguish
    "this broker does not report it" from "it is genuinely zero" — which is the exact
    failure that returned buying_power=0 for funded accounts.

    Asserting only "the response is 200 and has the field" passes with the bug.
    """
    info = AccountInfo(
        account=_ACCOUNT,
        account_type=AccountType.CASH,
        buying_power=_CONTROL_CASH,
        cash_balance=_CONTROL_CASH,
        equity=None,  # cash account: the venue reports no equity
    )
    client = _client(monkeypatch, _ReadAdapter(info=info))

    body = client.get(f"/accounts/{_ACCOUNT}?broker=liberator").json()

    assert body["equity"] is None, "an unreported field MUST be null, never 0"
    assert body["equity"] != 0
    assert body["initial_margin"] is None
    assert body["maintenance_margin"] is None


def test_money_crosses_the_wire_as_a_STRING_matching_the_control_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decimal-as-string at the money boundary, and the value cash-carry gave us.

    A float here would be a house-rule violation AND would silently re-quantise money.
    """
    info = AccountInfo(
        account=_ACCOUNT,
        account_type=AccountType.CASH,
        buying_power=_CONTROL_CASH,
        cash_balance=_CONTROL_CASH,
    )
    client = _client(monkeypatch, _ReadAdapter(info=info))

    body = client.get(f"/accounts/{_ACCOUNT}?broker=liberator").json()

    assert body["buying_power"] == "50000.11", "must equal the live control value"
    assert isinstance(body["buying_power"], str), "money is Decimal-as-string, never a float"


def test_derivative_account_carries_the_margin_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the asymmetry — DERIVATIVE reports what CASH cannot.

    Without this, the null-assertions above are satisfiable by a route that returns
    null for everything.
    """
    info = AccountInfo(
        account="70000007",
        account_type=AccountType.DERIVATIVE,
        buying_power=_CONTROL_DERIV,
        equity=_CONTROL_DERIV,
        initial_margin=Decimal("1000"),
        maintenance_margin=Decimal("700"),
    )
    client = _client(monkeypatch, _ReadAdapter(info=info))

    body = client.get("/accounts/70000007?broker=liberator").json()

    assert body["equity"] == "13000.22"
    assert body["initial_margin"] == "1000"
    assert body["account_type"] == "derivative"


def test_broker_is_REQUIRED_because_an_account_does_not_name_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``broker`` must 422, not default to a guess.

    Guessing a broker from an account number is the same class of invention that
    produced TK-0396.
    """
    info = AccountInfo(account=_ACCOUNT, account_type=AccountType.CASH, buying_power=_CONTROL_CASH)
    client = _client(monkeypatch, _ReadAdapter(info=info))

    assert client.get(f"/accounts/{_ACCOUNT}").status_code == 422


def test_an_unknown_account_is_404_not_a_fabricated_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The venue refusing an account must surface, never become buying_power=0."""
    client = _client(
        monkeypatch, _ReadAdapter(raises=LiberatorAccountNotFound("account not authorized"))
    )

    r = client.get(f"/accounts/{_ACCOUNT}?broker=liberator")

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "liberator_account_not_found"


# ----------------------------------------------------------------- open orders


def test_open_orders_returns_venue_truth(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _ReadAdapter(
        info=AccountInfo(account=_ACCOUNT, account_type=AccountType.CASH, buying_power=Decimal("1"))
    )
    adapter.open_orders = [make_order(broker="liberator", account=_ACCOUNT, symbol="KTB")]
    client = _client(monkeypatch, adapter)

    body = client.get(f"/accounts/{_ACCOUNT}/open-orders?broker=liberator").json()

    assert [o["symbol"] for o in body["orders"]] == ["KTB"]


def test_an_empty_venue_book_is_an_empty_LIST_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing resting is a legitimate answer and must read as one."""
    adapter = _ReadAdapter(
        info=AccountInfo(account=_ACCOUNT, account_type=AccountType.CASH, buying_power=Decimal("1"))
    )
    client = _client(monkeypatch, adapter)

    r = client.get(f"/accounts/{_ACCOUNT}/open-orders?broker=liberator")

    assert r.status_code == 200
    assert r.json() == {"orders": []}


# --------------------------------------------------------------- the guards hold


def test_an_UNDECLARED_account_is_refused_by_EH6_even_for_a_READ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 The read routes must not be a hole around EH6.

    They resolve through the same `_resolve_adapter` seam as `submit`, so a node not
    declared the real router for an account cannot read it either. That is arguably
    over-broad — EH6 exists to stop two nodes routing one account into double fills,
    and a read cannot double-fill — but it fails CLOSED, which is the side to err on,
    and it is asserted here so the coupling is deliberate rather than incidental.

    Without this test, someone "simplifying" the read path by calling the adapter
    directly would remove the guard and nothing would notice.
    """
    info = AccountInfo(account="9999999", account_type=AccountType.CASH, buying_power=Decimal("1"))
    client = _client(monkeypatch, _ReadAdapter(info=info))

    r = client.get("/accounts/9999999?broker=liberator")

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "real_routing_not_authorized"


def test_reads_are_OWNER_MODE_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Public mode must not expose real account financials.

    Same posture as every other venue-touching route in this file.
    """
    info = AccountInfo(account=_ACCOUNT, account_type=AccountType.CASH, buying_power=_CONTROL_CASH)
    client = _client(monkeypatch, _ReadAdapter(info=info), public_mode=True)

    for path in (
        f"/accounts/{_ACCOUNT}",
        f"/accounts/{_ACCOUNT}/open-orders",
        f"/accounts/{_ACCOUNT}/positions",  # added 2026-08-28 — holdings are financial data too
    ):
        r = client.get(f"{path}?broker=liberator")
        assert r.status_code == 403, f"{path} must be owner-mode only"
        assert r.json()["error"]["code"] == "public_mode"


# ---------------------------------------------- Liberator SET *and* TFEX, one path


class _ProfileTransport:
    """Stands in for the HTTP hop only — the REAL `_venue_result` / `_account_info` run.

    Every other test in this file injects a pre-built ``AccountInfo``, which cannot
    catch a mapping bug. This one replays the venue's actual ``/va/profile`` body so
    the adapter's own parse is what is under test.
    """

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.paths: list[str] = []

    async def get_json(self, path: str, **_: Any) -> dict[str, Any]:
        self.paths.append(path)
        return self._body


# SHAPE captured live from the AWS node's bridge 2026-08-27; VALUES redacted 2026-08-28.
#
# ⚠️ The FIELD SETS are still verbatim and that is the load-bearing part: the CASH entry
# genuinely OMITS equity/totalMr/totalMm while the DERIVATIVE entry REPORTS them as 0 —
# absent and zero are different facts, which is the whole of TK-0396.
#
# The account numbers and balances are SYNTHETIC. This repo is PUBLIC; real broker
# account numbers and balances live only in the private umbrella's
# docs/reference/liberator-account-reads.md. The synthetic accounts keep the venue's
# 8-digit <investorId><suffix> grammar (2 = CASH BALANCE, 7 = DERIVATIVE) because code
# reads market from that suffix.
_LIVE_PROFILE: dict[str, Any] = {
    "raw_response": {
        "errorCode": 0,
        "errMsg": "",
        "result": {
            "accounts": [
                {
                    "accountNo": "70000002",
                    "type": "CASH BALANCE",
                    "lineAvailable": 50000.11,
                    "cashBalance": 50000.11,
                    "creditLimit": 500000,
                    "withdrawAvailable": 50000.11,
                },
                {
                    "accountNo": "70000007",
                    "type": "DERIVATIVE",
                    "lineAvailable": 13000.22,
                    "cashBalance": 13000.22,
                    "creditLimit": 1000000,
                    "withdrawAvailable": 13000.22,
                    "equity": 13000.22,
                    "excessEquity": 13000.22,
                    "totalMr": 0,
                    "totalMm": 0,
                },
            ]
        },
    }
}


def _live_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _ProfileTransport]:
    from src.quant_execution_engine.adapters.liberator.adapter import LiberatorAdapter
    from src.quant_execution_engine.api import deps
    from src.quant_execution_engine.core.router import OrderRouter

    patch_repositories(monkeypatch, MemStore())
    transport = _ProfileTransport(_LIVE_PROFILE)
    adapter = LiberatorAdapter(
        transport=transport,  # type: ignore[arg-type]
    )
    settings = make_settings(
        public_mode=False,
        stage="micro_live",
        real_routing_accounts=["70000002", "70000007"],
    )
    order_router = OrderRouter(
        settings=settings, pool=object(), redis=FakeRedis(), liberator_adapter=adapter
    )
    app = create_app()
    app.dependency_overrides[deps.get_settings_dep] = lambda: settings
    app.dependency_overrides[deps.get_pool_dep] = lambda: object()
    app.dependency_overrides[deps.get_redis_dep] = lambda: FakeRedis()
    app.dependency_overrides[deps.get_router_dep] = lambda: order_router
    # Direct TestClient: must supply the api-key itself, since the guard now fails
    # closed without one ([[TK-0462]]).
    headers = {"X-API-Key": settings.api_key} if settings.api_key else {}
    return TestClient(app, headers=headers), transport


def test_liberator_SET_and_TFEX_balance_come_from_ONE_call_and_ONE_code_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔑 TFEX is not a separate branch — asserted, not argued.

    ``get_account`` issues ONE request to a constant path and selects the account by
    string equality inside a single ``accounts[]`` array. There is no market parameter
    and no per-market request, so a TFEX balance cannot be "an untested branch" at the
    request level. This asserts that structurally: **both accounts, same route, and the
    transport sees the SAME path both times.**
    """
    client, transport = _live_client(monkeypatch)

    set_body = client.get("/accounts/70000002?broker=liberator").json()
    tfex_body = client.get("/accounts/70000007?broker=liberator").json()

    assert set_body["buying_power"] == "50000.11"
    assert tfex_body["buying_power"] == "13000.22"
    assert set_body["account_type"] == "cash"
    assert tfex_body["account_type"] == "derivative"
    # One constant venue path for both — no market-specific endpoint.
    assert transport.paths == ["profile", "profile"], transport.paths


def test_the_TFEX_margin_block_distinguishes_ABSENT_from_ZERO_on_real_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 The one genuinely market-dependent branch, on the venue's real field sets.

    This is the strongest available evidence that `None` and `0` are not conflated,
    because the live payload contains BOTH cases at once:

      * the CASH entry OMITS `totalMr`/`totalMm` entirely  -> must be `null`
      * the DERIVATIVE entry REPORTS them with value 0     -> must be `0`, NOT null

    Same function, same request, real bytes. A mapping that collapsed either direction
    would fail here — and collapsing them is [[TK-0396]].
    """
    client, _ = _live_client(monkeypatch)

    set_body = client.get("/accounts/70000002?broker=liberator").json()
    tfex_body = client.get("/accounts/70000007?broker=liberator").json()

    # CASH: the venue never sent these -> null, and the model FORBIDS them here.
    assert set_body["initial_margin"] is None
    assert set_body["maintenance_margin"] is None
    assert set_body["equity"] is None

    # DERIVATIVE: the venue sent them, and their value is genuinely zero.
    assert tfex_body["equity"] == "13000.22"
    assert tfex_body["excess_equity"] == "13000.22"
    assert tfex_body["initial_margin"] == "0", "a REPORTED zero must not become null"
    assert tfex_body["maintenance_margin"] == "0"
    assert tfex_body["initial_margin"] is not None


def test_a_CASH_payload_carrying_margin_fields_is_IGNORED_not_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `if account_type is DERIVATIVE` guard, on the only data that can prove it.

    ⚠️ Found by mutation: replacing that guard with `if True` left every other test
    GREEN, because the live CASH payload simply omits equity/totalMr/totalMm, so
    reading them unconditionally is a no-op on real data. The guard was therefore
    UNTESTED — the mutation surviving is the finding, not a false alarm.

    The guard exists for the case the venue does not currently produce: a CASH entry
    that carries a margin field anyway. Without the guard those values reach
    `AccountInfo`, whose validator FORBIDS them on a non-derivative account, and the
    read 500s instead of returning a balance. With it, they are ignored.
    """
    from src.quant_execution_engine.adapters.liberator.adapter import LiberatorAdapter
    from src.quant_execution_engine.api import deps
    from src.quant_execution_engine.core.router import OrderRouter

    patch_repositories(monkeypatch, MemStore())
    hostile = {
        "raw_response": {
            "errorCode": 0,
            "errMsg": "",
            "result": {
                "accounts": [
                    {
                        "accountNo": "70000002",
                        "type": "CASH BALANCE",
                        "lineAvailable": 50000.11,
                        # the venue does not send these on a cash account — but if it
                        # ever did, they must not reach the model.
                        "equity": 999,
                        "totalMr": 888,
                    }
                ]
            },
        }
    }
    adapter = LiberatorAdapter(
        transport=_ProfileTransport(hostile),  # type: ignore[arg-type]
    )
    settings = make_settings(
        public_mode=False, stage="micro_live", real_routing_accounts=["70000002"]
    )
    app = create_app()
    app.dependency_overrides[deps.get_settings_dep] = lambda: settings
    app.dependency_overrides[deps.get_pool_dep] = lambda: object()
    app.dependency_overrides[deps.get_redis_dep] = lambda: FakeRedis()
    app.dependency_overrides[deps.get_router_dep] = lambda: OrderRouter(
        settings=settings, pool=object(), redis=FakeRedis(), liberator_adapter=adapter
    )

    _hdr = {"X-API-Key": settings.api_key} if settings.api_key else {}
    r = TestClient(app, headers=_hdr).get("/accounts/70000002?broker=liberator")

    assert r.status_code == 200, "the guard must absorb this, not 500 on a validator error"
    assert r.json()["buying_power"] == "50000.11"
    assert r.json()["equity"] is None, "a margin field on a CASH account must be dropped"
    assert r.json()["initial_margin"] is None


# ------------------------------------------------------- GET /accounts/{account}/positions


def test_positions_route_serialises_side_and_keeps_None_distinct_from_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``side`` must cross the wire as null when the venue did not distinguish.

    Coercing it to a string — "BUY", or worse "" — would assert a direction the venue
    withheld. A SET equity cannot be short and neither venue sends a side for one.
    """
    adapter = _ReadAdapter()
    adapter.positions = [
        Position(account=_ACCOUNT, market=Market.SET, symbol="AAA", net_qty=100, side=None),
        Position(account=_ACCOUNT, market=Market.TFEX, symbol="BBBZ26", net_qty=3, side=Side.SELL),
    ]
    client = _client(monkeypatch, adapter)

    r = client.get(f"/accounts/{_ACCOUNT}/positions?broker=liberator")

    assert r.status_code == 200
    rows = r.json()["positions"]
    assert rows[0]["side"] is None
    assert rows[1]["side"] == "SELL"
    assert rows[0]["market"] == "SET" and rows[1]["market"] == "TFEX"


def test_positions_route_is_refused_by_EH6_for_an_UNDECLARED_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positions route must not be a hole around EH6 either.

    It resolves through the same ``_resolve_adapter`` seam as every other read, so a node
    that is not the declared real router for an account cannot list its holdings.
    """
    client = _client(monkeypatch, _ReadAdapter())

    r = client.get("/accounts/9999999/positions?broker=liberator")

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "real_routing_not_authorized"


def test_an_UNREADABLE_account_does_NOT_come_back_as_an_empty_holding_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 The property this whole endpoint rests on.

    ``[]`` from this route must mean *"this account holds nothing"*. If a failure to READ
    could also produce ``[]``, the answer is worthless — "flat" is a plausible reading a
    caller will act on. Every unreadable path raises, and the raise must reach the client
    as an error status rather than an empty success.
    """
    adapter = _ReadAdapter()
    adapter.positions_raise = LiberatorAccountNotFound("cannot read that account")
    client = _client(monkeypatch, adapter)

    r = client.get(f"/accounts/{_ACCOUNT}/positions?broker=liberator")

    assert r.status_code == 404
    assert "positions" not in r.json(), "an unreadable account must never yield a holdings list"
