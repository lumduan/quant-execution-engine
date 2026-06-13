# Operations — Kill-switch

The kill-switch is the platform's hardest stop. It **overrides every stage**, is checked **first** in
the submit path, and on trip **mass-cancels every open order** (flatten-and-halt). This page is the
operator procedure; for the API see [`../api/admin.md`](../api/admin.md), for the design see
[`../architecture/security-boundary.md`](../architecture/security-boundary.md).

## What it does

- **Rejects all new submits** immediately (`kill_switch_engaged`, 503) — checked before idempotency,
  capability, risk, everything.
- **Gates the amend path too** (an amend can *increase* exposure). The **cancel** path is **not**
  gated — a cancel reduces risk, and the mass-cancel sweep uses it.
- **Mass-cancels every open order** on engage, returning `cancelled_count` + the `cancelled`/`failed`
  cid lists.

## Configuration

| Mechanism | How | When |
|-----------|-----|------|
| Boot backstop | `EXECUTION_ENGINE_KILL_SWITCH_ENGAGED=true` in `.env`, then restart | Pre-engage before the service ever accepts an order |
| Runtime trip | `POST /admin/kill-switch/engage` | Engage/disengage live without a restart |

When the env flag pins it on, a runtime disengage is refused (`409 kill_switch_env_pinned`) — clear
the env flag and restart to lift a boot-pinned switch.

## Engage (runtime)

```bash
curl -X POST http://localhost:8400/admin/kill-switch/engage \
  -H "X-API-Key: <your-api-key>" \
  -H "X-Operator-Id: ops-alice"
```

```json
{ "engaged": true, "already_engaged": false, "cancelled_count": 3,
  "cancelled": ["…", "…", "…"], "failed": [] }
```

- **Idempotent:** a second engage returns `already_engaged=true` with `cancelled_count=0` (no second
  sweep).
- A structured `kill_switch.engaged` JSON audit line is logged (operator + counts; never a secret).
- If `failed` is non-empty, those orders could not be cancelled — investigate the venue/breaker and
  re-engage (the sweep is best-effort and never stops early).

## Disengage

```bash
curl -X POST http://localhost:8400/admin/kill-switch/disengage \
  -H "X-API-Key: <your-api-key>" \
  -H "X-Operator-Id: ops-alice"
```

- `409 kill_switch_not_engaged` if the switch is already clear.
- `409 kill_switch_env_pinned` if it is boot-pinned by the env flag.
- Emits a `kill_switch.disengaged` audit line.

## Verify

```bash
curl http://localhost:8400/admin/kill-switch -H "X-API-Key: <your-api-key>"
# { "engaged": false, "source": null }
```

## The stage-flip rule (the most important operating rule)

**Always engage the kill-switch and verify the mass-cancel BEFORE flipping `EXECUTION_ENGINE_STAGE`;
never flip the stage while open orders exist.** A stage change must never race live orders. The
sequence for any `sim`/`paper` → `micro_live` (or back) transition:

1. `POST /admin/kill-switch/engage` → confirm `cancelled`/`failed` and that `failed` is empty.
2. Verify `GET /admin/kill-switch` shows `engaged: true` and `GET /health` shows no resting orders /
   healthy brokers.
3. Change `EXECUTION_ENGINE_STAGE` (and restart / reload as your deployment requires).
4. `POST /admin/kill-switch/disengage` to resume.

This rule is mirrored in the umbrella runbook
([`../../../.claude/playbooks/execution-engine-runbook.md`](../../../.claude/playbooks/execution-engine-runbook.md))
and the safety playbook ([`../../.claude/playbooks/order-routing-safety.md`](../../.claude/playbooks/order-routing-safety.md)).

## The 5-order fault test (Phase 6)

A Phase-6 fault-injection test engages the switch against **five** in-flight orders (a mix of `NEW`
and `PARTIALLY_FILLED`) and asserts (a) genuine `CANCELLED`-transition audit rows for each, and (b)
the structured `kill_switch.engaged` log with the correct `cancelled_count`. It proves the mass-cancel
actually walks the frozen cancel edges (not a status overwrite) and that the audit trail is real — the
evidence an operator relies on after an emergency stop.

## Circuit-breaker vs kill-switch

| | Kill-switch | Circuit breaker (per adapter) |
|---|---|---|
| Trigger | **operator** (engage) or boot flag | **automatic** — consecutive heartbeat failures |
| Scope | engine-wide (all brokers) | one broker (one OAuth app for Settrade per-market) |
| Effect | reject all new + mass-cancel | reject placements to that broker (`broker_circuit_open` 503) + mass-cancel its open orders |
| Clear | deliberate disengage | breaker recovers when the session heartbeat recovers |

They are independent. A tripped breaker does not engage the kill-switch, and vice-versa — but both end
the same way for the affected open orders: a mass-cancel.
