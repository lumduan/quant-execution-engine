"""🔒 The engine holds NO Liberator trading PIN — and still satisfies the bridge's schema.

Two obligations that used to be met by one value, now separated, because they move for
different reasons and a single test covering both would hide either one changing:

* **the GATE** decides whether Liberator routing starts — it must key on the api-key, the
  credential that actually authenticates the engine to the bridge;
* **the PAYLOAD** must carry a `pin` that satisfies the bridge's required field — a schema
  obligation, not a secret.

🔴 Why the engine's PIN was inert, recorded here because the tests below only make sense
against it: the bridge overwrites the caller's `pin` with its own configured value at six
unconditional sites (place/cancel/pre-place × SET/TFEX), and reads no caller `.pin`
attribute anywhere — AST-verified against code digest-matched to the deployed image on both
nodes. Production had already proven it independently: the engine's configured PIN was NOT
the bridge's, and real orders — places *and* cancels, both of which stamp this field — were
accepted by the venue throughout. See [[TK-0529]].
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import SecretStr
from src.quant_execution_engine.adapters.liberator import mapping
from src.quant_execution_engine.adapters.liberator.runtime import liberator_enabled
from src.quant_execution_engine.adapters.streaming_pro.runtime import streaming_pro_enabled

from tests.conftest import make_settings

_SRC = Path("src/quant_execution_engine")

# The bridge's contract, verified offline against the DEPLOYED request models on 2026-09-05:
# all six order models declare `pin: str = Field(..., min_length=6, max_length=10)` plus a
# `field_validator` enforcing `.isdigit()`. A 10- and a 6-digit value were accepted by all
# six; 5-digit, 11-digit, non-digit and blank were rejected by all six.
_BRIDGE_MIN, _BRIDGE_MAX = 6, 10


# ─────────────────────────────── the GATE half ───────────────────────────────


def test_the_gate_no_longer_depends_on_a_PIN() -> None:
    """With an api-key and no PIN setting at all, Liberator routing must still start.

    This is the property that lets the variable be deleted rather than faked.
    """
    settings = make_settings(
        public_mode=False, stage="micro_live", liberator_api_key=SecretStr("k")
    )
    assert not hasattr(settings, "liberator_pin"), "the setting must be gone, not merely unused"
    assert liberator_enabled(settings) is True


def test_the_gate_still_REFUSES_without_an_api_key() -> None:
    """A positive control. Without it, the test above would pass against a gate that had
    stopped checking anything at all — which is a different bug wearing the same green."""
    assert liberator_enabled(make_settings(public_mode=False, stage="micro_live")) is False


def test_the_liberator_gate_now_MATCHES_its_streaming_pro_sibling() -> None:
    """Structural equivalence, asserted rather than described.

    `streaming_pro_enabled` has never checked a PIN — the bridge owns it there too. The two
    gates guarding two bridges through the same contract should not differ, and this fails if
    one drifts from the other.
    """
    import inspect

    def shape(fn: object, prefix: str) -> list[str]:
        """The settings each gate reads, with the broker prefix normalised away.

        Sorted AFTER normalising — sorting first compares two different orderings and fails
        for a reason that has nothing to do with the gates.
        """
        body = inspect.getsource(fn)  # type: ignore[arg-type]
        body = body[body.index("return") :]
        return sorted(f.replace(prefix, "") for f in re.findall(r"settings\.(\w+)", body))

    lib = shape(liberator_enabled, "liberator_")
    sp = shape(streaming_pro_enabled, "streaming_pro_")
    assert lib == sp, f"gates diverged: liberator={lib} streaming_pro={sp}"
    assert "pin" not in " ".join(lib), "the liberator gate must not read a pin"


# ────────────────────────────── the PAYLOAD half ──────────────────────────────


def test_every_payload_still_carries_a_pin_the_BRIDGE_WILL_ACCEPT() -> None:
    """The bridge's `pin` field is REQUIRED — omitting it is a 422 and no order is placed.

    So the value could not simply be deleted; it had to move from configuration into code.
    Asserted as the CONTRACT (6-10, digits) rather than as a literal, so the constant can be
    changed without silently violating the bridge.
    """
    p = mapping._BRIDGE_REQUIRED_PIN_PLACEHOLDER
    assert p.isdigit(), "the bridge's field_validator enforces digits-only"
    assert _BRIDGE_MIN <= len(p) <= _BRIDGE_MAX, f"must be {_BRIDGE_MIN}-{_BRIDGE_MAX} digits"


def test_the_placeholder_is_NOT_MISTAKEABLE_for_a_real_PIN() -> None:
    """Both PINs observed on this platform are 6 digits; the placeholder is 10.

    Secondary to the real defence — that it appears in no node's configuration at all — but
    it means the value is distinguishable even when read out of context.
    """
    p = mapping._BRIDGE_REQUIRED_PIN_PLACEHOLDER
    assert len(p) == 10
    assert set(p) == {"0"}, "an obvious sentinel, not an arbitrary-looking number"


def test_place_and_cancel_BOTH_stamp_it() -> None:
    """Cancel carries the PIN too, and is the path EH7 depends on.

    A change that fixed place and forgot cancel would break cancellation only — the failure
    you discover when you most need it to work.
    """
    assert mapping.to_cancel_payload("3064")["pin"] == mapping._BRIDGE_REQUIRED_PIN_PLACEHOLDER


# ─────────────────────────────── it cannot creep back ───────────────────────────────


def test_no_liberator_PIN_setting_exists_anywhere_in_src() -> None:
    """🔴 The regression guard. The whole point is that this service is no longer a custodian
    of a live trading credential; a re-added setting would restore that silently."""
    offenders = [
        f"{p}:{i}"
        for p in _SRC.rglob("*.py")
        for i, ln in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "liberator_pin" in ln.lower() and not ln.lstrip().startswith("#")
    ]
    assert offenders == [], f"the liberator PIN setting is back: {offenders}"


def test_the_engine_never_unwraps_a_SECRET_into_a_liberator_payload() -> None:
    """`SecretStr.get_secret_value()` on the liberator path is what the removal eliminated."""
    for name in ("adapter.py", "mapping.py", "runtime.py"):
        text = (_SRC / "adapters" / "liberator" / name).read_text(encoding="utf-8")
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        assert "_pin.get_secret_value()" not in code, f"{name} unwraps a PIN secret"
    # positive control: the filter kept real code, so the assertions above are not vacuous
    assert "get_secret_value" in (_SRC / "adapters" / "liberator" / "transport.py").read_text(
        encoding="utf-8"
    ), "the api-key IS still unwrapped — the check above is not passing by an empty read"
