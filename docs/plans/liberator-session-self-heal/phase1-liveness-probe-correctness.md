# Phase 1: Liveness Probe Correctness

**Feature:** Liberator session self-heal — Phase 1: Liveness probe correctness
**Branch:** `feature/liberator-session-self-heal-phase1`
**Created:** 2026-06-13
**Status:** Complete
**Completed:** 2026-06-13
**Depends On:** [`ROADMAP.md`](ROADMAP.md) Phase 1 (prerequisite); decision **D2**

---

## Table of Contents

1. [Overview](#overview)
2. [AI Prompt](#ai-prompt)
3. [Scope](#scope)
4. [Design Decisions](#design-decisions)
5. [Implementation Steps](#implementation-steps)
6. [File Changes](#file-changes)
7. [Success Criteria](#success-criteria)
8. [Completion Notes](#completion-notes)

---

## Overview

### Purpose

The bundled `third_party/liberator-trading-api` ships a `SessionMonitorService` that can
auto-relogin a dead Liberator broker session, but it is **shipped disabled** because its
liveness probe is broken. `SessionStatusService.check_session_status` probed liveness with an
**order pre-place** (`POST /api/v1/order/pre-place/set`) carrying placeholder `accountNo` /
`pin`. Even on a *healthy* session those placeholder credentials make the pre-place fail, so the
probe reads "dead" — which would cause false-positive reconnect storms the moment the monitor is
enabled.

Phase 1 replaces that probe with a **read-only, auth-token-only** probe (`GET /api/v1/profile`).
The session is alive iff a stored auth token exists **and** the probe returns 2xx. This is the
prerequisite for the rest of the feature: detection must be trustworthy before auto-login can be
turned on. **The monitor stays disabled after this phase** (Phase 5 enables it).

### Parent Plan Reference

- [`docs/plans/liberator-session-self-heal/ROADMAP.md`](ROADMAP.md) — feature roadmap (D2, D5, D6)
- Engine ROADMAP: [`docs/plans/ROADMAP.md`](../ROADMAP.md)
- Cross-cutting: [`plans/feature-execution-engine/ROADMAP.md`](../../../../plans/feature-execution-engine/ROADMAP.md)

### Key Deliverables

1. **Read-only probe** — `check_session_status` calls `GET /api/v1/profile`; alive ⇔ stored token + 2xx.
2. **Drop placeholder-cred dependency** — no `accountNo` / `pin` anywhere in the probe path or config.
3. **Unit tests** — alive / 401 / 403 / no-token / network-error → correct `is_alive` & status.

---

## AI Prompt

The following prompt was used to generate this phase (verbatim):

```
## Objective

Implement Phase 1 — Liveness probe correctness of the Liberator session self-heal feature for
quant-execution-engine. This is a targeted fix inside the bundled
third_party/liberator-trading-api submodule: replace the broken
POST /api/v1/order/pre-place/set liveness probe (which requires placeholder account/PIN
credentials and will always read "dead" in the bundled deployment) with a read-only,
auth-token-only probe (GET /api/v1/profile) that correctly detects whether the stored JWT is
alive or dead.

No engine code changes. The execution engine (adapters/liberator/, adapters/session.py) is
untouched. No frozen NormalizedOrder contract, state machine, capability cells, gating, or
infra-db schema changes.

Context — The SessionStatusService probes liveness by posting to POST /api/v1/order/pre-place/set
with a test_payload that carries accountNo: '00000000' / pin: '000000'. Even on a healthy broker
session these placeholder credentials cause the probe to fail → false-positive "dead" readings.
The fix is to call GET /api/v1/profile instead — already implemented in the liberator service,
requires only the stored auth token, returns 2xx on a live session and 401/403 on an
expired/invalid token. The probe becomes self-contained: alive ⟺ stored token + 2xx. The monitor
stays disabled (session_monitor.enabled: false + auto_connect: false) after this phase.

Steps: read the mandatory docs; create a submodule-first dual-commit branch (D6); write this phase
plan doc; refactor app/services/session_status_service.py (GET /profile, status-based is_alive, no
test_payload); update app/models/session_status.py (mark SessionStatusTestPayload unused, keep it);
update docker/liberator/session_status.yaml (drop test_payload, monitor stays disabled); write unit
tests (alive-200 / dead-401 / dead-403 / no-token / network-error / no-placeholder-creds /
load-config-without-test-payload); run the quality gate; dual-commit + pin bump (D6); open two PRs;
report the ASCII git table.

Hard constraints: no engine code changes; no frozen-contract changes; dual-commit rule D6; never
log tokens/PINs/accounts; httpx.AsyncClient only (no requests); Pydantic V2; quality gate before
commit; monitor stays disabled after Phase 1; from __future__ import annotations at top of modified
modules; logger = get_logger(__name__) + %-formatting, no print.
```

> **Convention reconciliation (user direction: "do it in best practice", 2026-06-13).** Two of the
> stated hard constraints do not fit this submodule and were resolved in favour of *consistency
> with the surrounding code*:
> - **Logging** — the repo logs through **loguru structured kwargs**
>   (`logger.info("msg", key=val, component=...)`); `%`-formatting is not interpolated by loguru, so
>   the modified files keep the repo's structured-kwargs style.
> - **`from __future__ import annotations`** — **omitted** (0 of 73 `app/` modules use it; the repo
>   uses old-style `Optional[...]` / `Dict[...]` typing). Adding it to one or two files would be a
>   one-off inconsistency and risks Pydantic-v2 forward-ref resolution in the models module.

---

## Scope

### In Scope (Phase 1)

| Deliverable | Description | Status |
|---|---|---|
| Read-only liveness probe (D2) | `check_session_status` calls `GET /api/v1/profile`; alive ⇔ 2xx with a valid auth token | Complete |
| Drop placeholder-cred dependency | Remove the `accountNo` / `pin` `test_payload` from the probe path + the default-payload block + the overlay config | Complete |
| Unit tests | alive-200 / dead-401 / dead-403 / no-token / network-error / no-placeholder-creds / load-config-without-test_payload | Complete |

### Out of Scope (later phases)

- Config-schema consolidation — collapse the duplicate `session_status.monitoring` / `session_monitor` blocks (**Phase 2**)
- Single-flight lock + exponential backoff + trading-hours respect for re-login (**Phase 3**)
- Fail-loud OTP-timeout alerting (**Phase 4**)
- Enabling the monitor + end-to-end self-heal verification (**Phase 5**)
- Operator runbook / ops docs (**Phase 6**)
- The repo-wide pre-existing test/lint debt (241 failing tests, 1279 ruff findings) — a separate effort

---

## Design Decisions

Drawn from ROADMAP **D2** (read-only liveness probe).

1. **Read-only probe.** A liveness check must not be order-shaped. `GET /api/v1/profile` reads the
   broker profile with only the stored auth token — no order, no account, no PIN.
2. **Why `GET /api/v1/profile`.** It is already implemented (`app/api/endpoints/profile.py` +
   `app/services/profile_service.py`), needs only the api-key (locally) + the Redis auth token
   (forwarded to the broker), and maps liveness cleanly: 2xx → alive, 401/403 → token rejected,
   503/transport → dead.
3. **The probe targets the *local* API, not the broker directly.** `SessionStatusService._base_url`
   is `http://{api_host}:{api_port}` (the service's own FastAPI). The previous probe posted to the
   *local* `/api/v1/order/pre-place/set`; the fix is a drop-in swap to the *local* `/api/v1/profile`,
   which internally retrieves the Redis token and calls the broker. So the local endpoint's HTTP
   status already reflects broker-session liveness.
4. **No account/PIN in config.** The probe is self-contained, so the `test_payload` block is removed
   from the probe path, the default-configuration block (which hard-coded `accountNo` / `pin` in
   source), and the mounted overlay (`docker/liberator/session_status.yaml`).
5. **`is_alive` semantics.**
   - stored token + 2xx → `is_alive=True`, `status="alive"`
   - 401 / 403 → `is_alive=False`, `status="dead"`, `error_details="auth_token_rejected: HTTP {code}"`
   - other non-2xx → `is_alive=False`, `status="dead"`, `error_details="probe_http_error: HTTP {code}"`
   - transport failure (`httpx.RequestError`) → `is_alive=False`, `status="dead"`, `error_details="probe_network_error: …"`
   - no stored token → `is_alive=False`, `status="authentication_error"` (no probe issued)
   - The status code is inspected directly — `raise_for_status()` is **not** called, so 401/403 yield
     a clean "dead" reading rather than an exception path.
6. **`SessionStatusTestPayload` is kept** (not deleted) — it is still the type of the retained
   `SessionStatusResponse.test_payload_used` field (always `None` from the probe now) and is used by
   an API test. It is annotated unused-by-probe and slated for Phase 2 removal.

---

## Implementation Steps

1. **`app/services/session_status_service.py`** — replace `_pre_place_endpoint` with
   `_profile_endpoint = "/api/v1/profile"`; rewrite `check_session_status` to issue the read-only GET
   and map `is_alive` from the status code (per Design §5); drop `_test_payload` (the attribute, its
   load in `_load_configuration`, and the default-payload block in `_use_default_configuration`);
   update `health_check` to report the profile endpoint and `config_loaded = hasattr(self,
   "_service_config")`; refresh the module/class docstrings. httpx is retained; tokens/headers are
   never logged.
2. **`app/models/session_status.py`** — annotate `SessionStatusTestPayload` as unused-by-probe;
   keep the class and the `SessionStatusResponse.test_payload_used` field.
3. **`docker/liberator/session_status.yaml`** (engine) — remove the `session_status.test_payload`
   block; keep `session_monitor.enabled: false` + `auto_connect: false`; document the read-only probe.
4. **`tests/test_services/test_session_status_service.py`** — rewrite the probe tests to mock httpx
   (`unittest.mock`, no respx) instead of the stale curl_cffi `AsyncSession`; add the required cases
   plus branch-coverage cases; preserve the non-probe tests (fix `close_session` → `aclose`).
5. **`tests/test_api/test_session_status.py`** — make the api-key-protected `/session/status` tests
   pass by patching `app.services.otp_sms_service.validate_api_key` (the canonical repo pattern;
   without it the hardened `verify_api_key` fails closed with 503 when the server `API_KEY` is unset);
   refresh the stale health-fixture strings.

---

## File Changes

### `third_party/liberator-trading-api` (submodule)

| File | Action | Description |
|---|---|---|
| `app/services/session_status_service.py` | MODIFY | Read-only `GET /api/v1/profile` probe; status-based `is_alive`; drop `_test_payload`; `health_check` + docstrings updated |
| `app/models/session_status.py` | MODIFY | Annotate `SessionStatusTestPayload` unused-by-probe (kept; still types `test_payload_used`) |
| `tests/test_services/test_session_status_service.py` | REWRITE | httpx-mocked probe tests (24 cases) — replaces stale curl_cffi mocks |
| `tests/test_api/test_session_status.py` | MODIFY | Patch `validate_api_key`; refresh stale health fixture (14 cases green) |

### `quant-execution-engine` (engine)

| File | Action | Description |
|---|---|---|
| `docker/liberator/session_status.yaml` | MODIFY | Drop `test_payload`; monitor stays disabled; document the read-only probe |
| `docs/plans/liberator-session-self-heal/phase1-liveness-probe-correctness.md` | CREATE | This plan document |
| `third_party/liberator-trading-api` | BUMP | Submodule pin → the pushed Phase-1 SHA (D6) |

---

## Success Criteria

- [x] The liveness probe needs no account number or PIN and reads correctly on a healthy session.
- [x] alive (2xx) / dead (401, 403) / dead (other HTTP) / dead (network) / no-token
      (authentication_error) all produce the correct `is_alive` and `status`.
- [x] No `accountNo` / `pin` in the probe request (structural test) or in any config the probe reads.
- [x] `SessionStatusTestPayload` retained (still types `test_payload_used`); probe always sets it `None`.
- [x] Monitor stays disabled — `session_monitor.enabled: false` + `auto_connect: false`.
- [x] No engine `src/` change; no frozen `NormalizedOrder` / state machine / capability cells /
      gating / infra-db change (D5).
- [x] Both session-status test files green (24 + 14 = 38); ≥90% coverage on `session_status_service`
      (91%); mypy-clean on the two `src` modules; the rewritten files are ruff-format clean.
- [x] No regression to the broader (pre-existing-red) suite.
- [ ] Dual-commit + pin bump (D6) — performed at PR time.

---

## Completion Notes

### Summary

Implemented as designed. `check_session_status` now issues a read-only `GET /api/v1/profile` and
derives `is_alive` purely from the stored-token presence + the response status code; the
order-pre-place probe, its `test_payload`, the hard-coded default credentials, and the overlay
`test_payload` block are all gone. The monitor remains disabled. Coverage on
`app/services/session_status_service.py` is **91%**; mypy is clean on the two `src` modules; the
rewritten files are ruff-format clean.

### Issues encountered / findings

1. **The probe targets the local API, not the broker.** Confirmed via `_base_url` + the existing
   pre-place probe — the fix is a clean drop-in to the local `/api/v1/profile` route, which already
   maps broker-session liveness to its HTTP status.
2. **The service used httpx, but its tests were stale curl_cffi.** `session_status_service.py`
   already used `httpx.AsyncClient`, yet the test file mocked curl_cffi's `AsyncSession`, asserted the
   broker URL + a `Bearer` header, and called `close()` (httpx uses `aclose()`). The probe tests were
   fully rewritten to httpx mocks.
3. **The bundled submodule is broadly pre-existing-red.** Baseline on `main`: **241 failing tests /
   11 errors** and **1279 ruff findings** (no `[tool.ruff]` config; the codebase universally uses
   `timezone.utc` / `Optional[...]` / `Dict[...]`). The full-suite "pass cleanly" / `ruff check .`
   gates are therefore not achievable by a targeted Phase-1 change. Per the approved plan, this PR is
   accountable only for the **session-status files** + **no regression**: failures dropped 241 → 225
   (the 16 session-status failures fixed) and passes rose 558 → 578 (16 + 4 net-new tests); errors
   unchanged. The repo-wide debt is out of Phase-1 scope (candidate for a separate rehabilitation PR).
4. **`verify_api_key` fails closed (503).** The hardened api-key dependency raises 503 when the
   server `API_KEY` is unset (as in tests); the `/session/status` API tests had never been updated to
   patch `validate_api_key`, so they 503'd. Fixed with the canonical repo pattern.
5. **`SessionMonitorService._should_trigger_immediate_login` (Phase 3 territory).** It string-matches
   old probe error text (`"HTTP Error 401"`, `"Failed to validate SET order pre-place API response"`),
   which the new `error_details` (`"auth_token_rejected: HTTP 401"`) won't match. Harmless for Phase 1
   (the monitor is disabled and `is_alive` is the real signal); to be updated when re-login is
   hardened in Phase 3.

---

**Document Version:** 1.0
**Author:** AI Agent (Claude Opus 4.8)
**Status:** Complete
**Completed:** 2026-06-13
