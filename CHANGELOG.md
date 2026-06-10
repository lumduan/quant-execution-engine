# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (feature-execution-engine — Phase 2: engine core + SimAdapter)

- **The full sim order path over the Phase-1 store** (no real-money path exists):
  - `contracts/` — frozen `NormalizedOrder`/`NormalizedOrderResult` (UUIDv4 boundary
    validation, no-float money, cross-field rules, Decimal-as-string wire, additive
    `engine_state`), frozen enums, static capability matrix, typed error taxonomy.
  - `core/` — pure 13-edge state machine; PTRM `RiskGate` (qty/notional caps,
    per-second rate + duplicate-burst via Redis, stage-aware fail policy);
    `KillSwitch` (env flag + runtime Redis trip, env pins); stage ladder;
    `OrderRouter` (kill-switch-first pipeline, dedupe → prior result, single-flight
    lock with PK backstop, §B-atomic ack, synchronous deterministic fills, IOC
    cancel walk, best-effort mass-cancel).
  - `adapters/` — frozen 7-method `BrokerAdapter` ABC, circuit-breaker scaffolding
    (§G, inert for sim), deterministic `SimAdapter` (`sim_fills`/`sim_reject`
    metadata control channel, FOK/IOC semantics, `SIM-`/`SIMF-` id synthesis).
  - `db/` — asyncpg pool singleton, typed rows, repositories (never write
    `order_events`; SQLSTATE 23514 → `IllegalTransition`).
  - `cache/` — Redis singleton, SET-NX-EX single-flight with Lua release, counters.
  - `api/` — app factory + resilient lifespan; `POST /orders`,
    `GET`/`DELETE /orders/{client_order_id}`, `GET /capabilities`, `GET /health`,
    owner-mode `/admin/kill-switch*`; hmac API-key + public-mode guards; uniform
    `{"error": {code, message, …}}` envelope.
- Tests: 104 passing, ~99% coverage; `integration` marker excluded by default.
- `.env.example`: PTRM caps, submit-lock, and sim knobs.

### Added
- Initial template scaffold: `src/`, `tests/`, `docs/`, `.claude/`, `.github/`.
- `pyproject.toml` with `ruff`, `mypy`, `pytest`, `pytest-asyncio`, `pytest-cov`, `bandit`, `pip-audit`.
- Multi-stage `Dockerfile` (uv-native, Python 3.11-slim).
- CI workflow (lint, format check, type check, test with coverage) on Python 3.11 and 3.12.
- Docker publish workflow targeting GHCR.
- Weekly security scan workflow (`bandit` + `pip-audit`).
- AI-agent enablement: `.claude/knowledge/project-skill.md`, `.claude/playbooks/feature-development.md`, `.claude/prompts/Prompt-Engineer.prompt.md`.
- Issue templates (bug, feature), PR template, `FUNDING.yml`.

[Unreleased]: https://github.com/OWNER/REPO/compare/HEAD...HEAD
