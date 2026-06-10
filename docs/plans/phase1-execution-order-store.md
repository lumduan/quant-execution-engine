# Phase 1: `quant-infra-db` `execution` Order Store

**Feature:** feature-execution-engine — Phase 1: durable `execution` order store
**Branch:** `feat/execution-schema` (quant-infra-db) / `docs/phase1-execution-order-store` (this repo)
**Created:** 2026-06-10
**Status:** Complete
**Completed:** 2026-06-10
**Depends On:** Phase 0 (Complete — ADR ACCEPTED 2026-06-10, contracts frozen)

## Table of Contents

1. [Overview](#overview)
2. [AI Prompt](#ai-prompt)
3. [Scope](#scope)
4. [Design Decisions](#design-decisions)
5. [Schema](#schema)
6. [Implementation Steps](#implementation-steps)
7. [File Changes](#file-changes)
8. [Success Criteria](#success-criteria)
9. [Completion Notes](#completion-notes)

## Overview

### Purpose

Ship the durable, idempotent, auditable order store that Phase 2 (engine core +
`SimAdapter`) builds on: `db_execution` / `execution.{orders, fills, order_events}` in
`quant-infra-db`, encoding the **frozen** Phase-0 contracts (`NormalizedOrder` enums, the
9-state order machine, ADR §A idempotency and §B reconciliation pins) directly in the
database.

### Parent Plan Reference

- Per-service ROADMAP: [`ROADMAP.md`](ROADMAP.md) — Phase 1 section (scope/acceptance
  realised verbatim).
- Cross-cutting: umbrella `plans/feature-execution-engine/ROADMAP.md` Phase 1.
- The ADR (Phase-0 gate, ACCEPTED): umbrella
  `.claude/knowledge/feature-execution-engine.md`.
- Schema-PR precedent: marketdata-engine Phase 1
  (`quant-infra-db/init-scripts/10_schema_market_data.sql`).

### Key Deliverables

- `quant-infra-db/init-scripts/12_schema_execution.sql` — fully idempotent; `db_execution`
  + `execution` schema + 3 tables + 3 trigger functions / 4 triggers + 3 indexes.
- `db_execution` added to `01_create_databases.sql` (`\gexec` guard).
- `execution_dsn` property in `quant-infra-db/src/config.py` + unit test.
- `quant-infra-db/tests/test_execution_schema.py` — 14 live-DB infra tests
  (`-m infra`).
- Live-applied to the running `quant-postgres` on `quant-network` (twice; second run
  no-op).
- Docs: infra-db CHANGELOG / CLAUDE.md / docs; this plan; ROADMAP + knowledge status
  flips here and in the umbrella.

## AI Prompt

The following prompt was used to generate this phase:

```
🎯 Objective

Implement **Phase 1 — `quant-infra-db` `execution` order store** of the
`feature-execution-engine` initiative in the quant-trading-system umbrella repo
(`/home/batt/docker/quant-trading-system`). Phase 0 (design/ADR gate) is COMPLETE and the
contracts are FROZEN — your job is to ship the durable, idempotent, auditable order store
that Phase 2 (engine core + SimAdapter) will build on, plan-first, across the correct repos,
with PRs to GitHub.

⚠️ Repo topology — read before touching anything

This umbrella repo is a meta repo of **git submodules**. The Phase 1 *code* (SQL schema +
tests) lands in the `quant-infra-db/` sub-repo in its **own** PR. The Phase 1 *plan document*
and roadmap/status updates land in the `quant-execution-engine/` sub-repo in its **own** PR.
The umbrella only ever bumps submodule pins + cross-cutting docs — **never commit into a
submodule from the umbrella**. Submodules sit on detached HEAD after `git submodule update`;
ALWAYS `git -C <path> switch main && git -C <path> pull` then create a feature branch before
any work inside one.

📖 Required reading (in this order, before planning)

1. `CLAUDE.md` (umbrella system map: network contract, cross-cutting rules, feature registry)
2. `quant-execution-engine/CLAUDE.md` (service context, hard rules, frozen contract summary)
3. `quant-execution-engine/docs/plans/ROADMAP.md` — **source of truth**; Phase 1 scope,
   acceptance, and quality gate are specified verbatim in its "Phase 1" section
4. `.claude/knowledge/feature-execution-engine.md` (the ACCEPTED ADR — D1–D13, pinned §A–§G)
5. `quant-execution-engine/.claude/knowledge/order-state-machine.md` and
   `quant-execution-engine/.claude/knowledge/normalized-order-contract.md` (frozen 9-state
   machine + field/enum definitions the schema must encode exactly)
6. `quant-infra-db/CLAUDE.md` + `quant-infra-db/init-scripts/` — study
   `01_create_databases.sql` and `10_schema_market_data.sql` (the marketdata Phase 1 schema PR
   is the explicit precedent for naming, idempotency style, DB provisioning, and live-apply)
7. `strategies/csm-set/docs/plans/examples/phase1-sample.md` — the required plan-doc format

📝 Step 1 — Plan before code

Write the implementation plan FIRST as
`quant-execution-engine/docs/plans/phase1-execution-order-store.md` (matches the existing
`phase0-design-adr-gate.md` naming). Follow the phase1-sample format: header block
(Feature / Branch / Created 2026-06-10 / Status / Depends On: Phase 0 Complete), TOC,
Overview, **AI Prompt section containing this prompt verbatim in a fenced block**, Scope,
Design Decisions, Implementation Steps, File Changes, Success Criteria, Completion Notes
(filled in at the end). Design decisions you must take a documented stance on:

- Database vs schema: the frozen docs say the store is `execution.*` reachable in
  `db_execution` on `quant-postgres` — confirm against how `db_market_data` +
  `market_data` schema were provisioned and mirror that pattern.
- Plain tables vs TimescaleDB hypertables: orders/fills/events are LOW-volume real-money
  rows, not tick data — justify whichever you choose; do not cargo-cult the OHLCV design.
- Enum encoding (Postgres ENUM vs CHECK constraint) for `status`, `side`, `order_type`,
  `tif`, `market`, `position_effect` — must cover the full frozen sets including the three
  local states `PENDING_NEW | PENDING_CANCEL | PENDING_REPLACE` plus
  `NEW | PARTIALLY_FILLED | FILLED | CANCELLED | REJECTED | EXPIRED`.
- How the legal-transition guard is enforced (trigger and/or app-level; ROADMAP allows
  "app-level and/or trigger" — if trigger, it must encode the exact frozen transition graph,
  terminal states immutable).

🗄️ Step 2 — Schema implementation (`quant-infra-db`, branch e.g. `feat/execution-schema`)

New init script `quant-infra-db/init-scripts/12_schema_execution.sql` (next free NN), fully
**idempotent** (re-runnable on a live database: `IF NOT EXISTS` / `CREATE OR REPLACE` / DO
blocks), shipping exactly the Phase 1 scope from the ROADMAP:

- `execution.orders` — **PK on `client_order_id`** (the idempotency constraint; UUIDv4 per
  ADR §A, store as `uuid` or validated `text` — justify), `broker` (`sim|liberator|settrade`),
  `broker_order_id` (nullable until ack; the §B mapping column), `account`, `symbol`,
  `market` (`SET|TFEX`), `side` (`BUY|SELL`), `order_type` (8 frozen values), `price` /
  `stop_price` **`numeric(18,6)`** (NEVER float/real), `quantity` / `display_qty` integers
  with positivity checks, `tif` (`DAY|IOC|FOK|GTC`), `position_effect`
  (`OPEN|CLOSE|NULL`), `status` (9 states), `created_at` / `updated_at` **`timestamptz`
  UTC**. Index `broker_order_id` and `(account, symbol, side, quantity, created_at)` to
  serve the ±5 s reconciliation fuzzy-match pinned in ADR §B.
- `execution.fills` — FK → orders, fill qty + `numeric(18,6)` price, broker fill/trade id,
  `timestamptz`; supports partial + total executions and avg-fill derivation.
- `execution.order_events` — **append-only audit trail**: FK → orders, from-status,
  to-status, event payload (`jsonb`), `timestamptz`; enforce append-only (no UPDATE/DELETE —
  trigger or revoked privileges) and reject illegal transitions per the frozen state machine.
- Wire `db_execution` provisioning the same way `01_create_databases.sql` +
  `10_schema_market_data.sql` did for `db_market_data` (roles/grants consistent with
  existing scripts; least privilege — strategies/gateway get NO write grant on `execution.*`).

Also in this repo, following its existing conventions: tests (e.g.
`quant-infra-db/tests/test_execution_schema.py`) matching the patterns in
`quant-infra-db/tests/conftest.py` — cover: script applies idempotently (twice), duplicate
`client_order_id` rejected, illegal transition rejected (e.g. `FILLED → NEW`), legal
lifecycle walk succeeds, `order_events` append-only holds, enum/check constraints reject bad
values, `numeric` precision round-trips Decimal. Mark/skip live-DB tests exactly as existing
infra-db tests do so the default suite stays hermetic. Update `quant-infra-db/CHANGELOG.md`,
`quant-infra-db/docs/` (schema reference), and its `CLAUDE.md` if it enumerates schemas.
Document the **live-apply runbook step** (init-scripts only auto-run on a fresh volume;
state the exact `psql` apply command against the running `quant-postgres` on
`quant-network`, mirroring how schema 10 was rolled out).

Quality gate before push (infra-db's own gate): `uv run ruff check . && uv run ruff format
--check . && uv run mypy ... && uv run pytest` — all via `uv run`, never bare python/pip.
If any sed/manual edit happens after formatting, re-run `ruff format --check` before push.

📚 Step 3 — Docs / knowledge / status updates

- `quant-execution-engine` (branch e.g. `docs/phase1-execution-order-store`): the plan doc
  from Step 1; flip ROADMAP Phase 1 status `[ ]` → `[x]` with date + shipped summary;
  update the "Current state" banner in `quant-execution-engine/CLAUDE.md` (Phase 1 complete,
  next: Phase 2); update `quant-execution-engine/.claude/knowledge/*` where the schema
  realisation adds durable facts (e.g. state-machine doc gains the DB-enforcement note).
- Umbrella (branch e.g. `docs/phase1-execution-order-store`): update the
  `feature-execution-engine` rows in `CLAUDE.md` (Optional Features table + companion-docs
  bullet), `plans/feature-execution-engine/ROADMAP.md` Phase 1 status, and
  `.claude/knowledge/feature-execution-engine.md` / `optional-features-registry.md` status
  lines. Keep umbrella edits docs-only.

🔀 Step 4 — Commit + PR sequencing (Conventional Commits everywhere)

1. PR ① `quant-infra-db` (code) — e.g. `feat: add execution order store schema (Phase 1)`.
2. PR ② `quant-execution-engine` (plan + status docs).
3. Umbrella: **only after ①+② are merged** to their integration branches (`main` for both),
   advance the two submodule pins explicitly (`git -C <path> fetch && git -C <path> checkout
   origin/main`, `git add <path>`, `chore: bump <path> pin to <sha>`) together with the
   Step 3 umbrella doc updates → PR ③. Pins never advance implicitly; never point a pin at
   an unmerged branch SHA. If you cannot merge ①/② yourself, open all PRs, leave ③ as a
   branch referencing them, and say so explicitly in your final report.

After every commit/push/PR, report results as the user's standard ASCII box-drawing table
(Repo | Branch | Commit | GitHub).

🚫 Non-goals / guardrails

- NO engine code, NO adapters, NO FastAPI routes, NO broker calls — Phase 1 is schema only.
- No strategy-specific columns; no secrets in any repo or SQL; never log/commit credentials.
- Do not edit submodule history from the umbrella; do not touch `quant-dashboard` (archived).
- Do not weaken any existing init script; script 12 must not break a fresh
  `docker compose up` of quant-infra-db.

✅ Acceptance (from the ROADMAP — all must hold)

- `12_schema_execution.sql` applies idempotently against the live `quant-postgres` on
  `quant-network` (run it twice; second run is a no-op).
- A duplicate `client_order_id` insert is rejected by the PK/unique constraint.
- An illegal state transition is rejected; a legal full lifecycle
  (PENDING_NEW → NEW → PARTIALLY_FILLED → FILLED) succeeds and leaves one
  `order_events` row per transition.
- `db_execution` / `execution.*` reachable as `quant-postgres` from `quant-network`.
- infra-db quality gate green; all tests pass; plan doc exists with this prompt embedded;
  ROADMAP/CLAUDE/knowledge statuses updated; PRs opened per the sequencing above.
```

## Scope

### In Scope

| Item | Status |
|---|---|
| `db_execution` database (`\gexec` guard in `01_create_databases.sql`) | ✅ |
| `init-scripts/12_schema_execution.sql` — fully idempotent | ✅ |
| `execution.orders` — idempotency PK + frozen enums as CHECKs + cross-field CHECKs | ✅ |
| `execution.fills` — FK → orders, at-least-once fill dedupe | ✅ |
| `execution.order_events` — append-only audit trail | ✅ |
| Legal-transition guard trigger (exactly the 13 frozen edges) | ✅ |
| Audit auto-append trigger (one row per INSERT/transition) | ✅ |
| §B reconciliation index + partial `broker_order_id` index | ✅ |
| `execution_dsn` config property + unit test | ✅ |
| 14 live-DB infra tests (`-m infra`) | ✅ |
| Live apply to running `quant-postgres` (twice) | ✅ |
| infra-db CHANGELOG / CLAUDE.md / docs updates | ✅ |
| Status flips (this repo + umbrella) | ✅ |

### Out of Scope

- Engine code, adapters, FastAPI routes, broker calls (Phase 2+).
- Pydantic row models in infra-db — the `NormalizedOrder` Pydantic contract is owned by
  this repo and lands in Phase 2 (a second copy in infra-db would be a drift hazard).
- `reject_reason` / `metadata` columns (not in the frozen Phase 1 column list; Phase 2 can
  `ALTER TABLE` or source from `order_events.event`).
- Per-service DB roles/grants — only the `postgres` superuser exists in the stack today;
  the least-privilege plan is documented in the script header for when Phase 2 adds an
  `execution_engine` role (strategies/gateway get **no** write grant on `execution.*`).
- State-machine edges beyond the frozen 13 (see Design Decision 5).

## Design Decisions

1. **Dedicated `db_execution` + `execution` schema.** Mirrors the `db_market_data`
   precedent exactly (dedicated DB so the store is independently owned by its engine);
   the ROADMAP acceptance names `db_execution` literally. Cost: one DSN property + one
   `\gexec` line.
2. **PLAIN tables — no TimescaleDB.** Orders/fills/events are the low-volume real-money
   command plane (tens of rows/day), not tick data. `fills` and `order_events` must FK to
   `orders` and **hypertables cannot be FK targets**; chunking/compression buy nothing and
   retention machinery would actively endanger audit rows. `db_execution` gets no
   timescaledb extension; `02_enable_timescaledb.sql` untouched.
3. **TEXT + CHECK constraints, not Postgres ENUM types.** Matches the script-10 precedent;
   idempotent under re-run with zero ceremony (Postgres has no `CREATE TYPE IF NOT
   EXISTS`); evolution is `DROP/ADD CONSTRAINT` vs `ALTER TYPE`'s restrictions (value
   removal effectively impossible); violations raise `check_violation` (23514) →
   `asyncpg.exceptions.CheckViolationError`, the infra suite's existing idiom.
4. **`client_order_id` is `TEXT` with only a length sanity CHECK (8–64).** ADR §A is
   explicit: the id is opaque — "stored, compared, never parsed" — format-validated **at
   the boundary** (the engine, Phase 2), and ULID/Snowflake are sanctioned drop-in upgrades
   that do not fit a `uuid` column. A DB-side UUID-format CHECK would contradict both the
   validation placement and the upgrade clause.
5. **Transition guard = DB trigger encoding EXACTLY the frozen 13-edge graph.** Phase 1
   has no app, so a trigger is the only live enforcement (ROADMAP allows "app-level and/or
   trigger"; Phase 2 adds the app-level guard on top). `orders_guard` (BEFORE INSERT OR
   UPDATE): INSERT must enter at `PENDING_NEW` (single entry node — entry at `NEW` would
   fabricate an unaudited ack); UPDATE validates `(OLD.status → NEW.status)` against the
   13 frozen edges with ERRCODE 23514; terminal immutability falls out of the edge list;
   same-status updates pass without an event (second partial fills are audited in
   `fills`); `updated_at` auto-maintained. **Known frozen-graph gaps deliberately NOT
   encoded** (the frozen contract wins over pragmatics): no venue-cancel-reject edge, no
   fills-while-`PENDING_CANCEL`, no `PENDING_REPLACE → CANCELLED` (the Liberator
   cancel+replace amend path models the old order's end state) — Phases 2/3 must amend the
   ADR first, then ship a follow-up migration.
6. **Audit rows are trigger-written, not app-supplied.** `orders_append_event`
   (AFTER INSERT OR UPDATE) appends exactly one `order_events` row per INSERT (birth row
   `NULL → PENDING_NEW`) and per status transition, with a `jsonb` payload snapshotting
   `broker_order_id`/price/quantity — which makes ADR §B's "mapping persisted atomically
   with `PENDING_NEW → NEW`" a DB invariant (the ack UPDATE sets `status` and
   `broker_order_id` in one statement; the same transaction writes the audit row).
7. **`order_events` append-only via statement-level BEFORE UPDATE/DELETE/TRUNCATE triggers
   raising P0001.** Grants cannot enforce anything while only the `postgres` superuser
   exists, so the triggers ARE the enforcement. FK side effect: an order with events can
   never be deleted — the audit chain protects its subject.
8. **`fills` dedupe = `UNIQUE (client_order_id, broker_fill_id)`.** At-least-once fill
   delivery (ADR §A) means redelivery must collide. NULL `broker_fill_id` rows are exempt
   (Postgres NULLs are distinct) — documented caveat: adapters must supply a fill id
   (`SimAdapter` synthesises one). Identity bigint PK (single writer; insertion-ordered).
9. **Indexes for §B reconciliation.** Partial `(broker, broker_order_id) WHERE
   broker_order_id IS NOT NULL` (venue→local lookup; not unique — venue order numbers can
   be day-scoped/recycled) + `(account, symbol, side, quantity, created_at)` — verbatim
   the §B fuzzy-match key with `created_at` last for the ±5 s range scan.
10. **Python scope: `execution_dsn` only.** The marketdata Phase 1 shipped row
    models/repositories because its consumers read through infra-db helpers; the execution
    engine owns its own contract models (Phase 2). Coverage gate holds via the
    `test_execution_dsn_format` unit test.

## Schema

```
db_execution
└── execution
    ├── orders        client_order_id PK (TEXT, len 8–64) │ broker CHECK(sim|liberator|settrade)
    │                 broker_order_id NULL-until-ack │ account │ symbol │ market CHECK(SET|TFEX)
    │                 side CHECK(BUY|SELL) │ order_type CHECK(8 frozen) │ price/stop_price numeric(18,6)
    │                 quantity/display_qty BIGINT>0 │ tif CHECK(DAY|IOC|FOK|GTC)
    │                 position_effect CHECK(OPEN|CLOSE|NULL, TFEX-only) │ status CHECK(9 frozen)
    │                 created_at/updated_at timestamptz │ cross-field CHECKs (price requiredness,
    │                 display_qty ≤ quantity) │ idx: (broker,broker_order_id) partial,
    │                 (account,symbol,side,quantity,created_at)
    ├── fills         fill_id identity PK │ FK → orders │ broker_fill_id │ price numeric(18,6)>0
    │                 quantity BIGINT>0 │ exec_ts/created_at timestamptz │ UNIQUE(client_order_id,broker_fill_id)
    ├── order_events  event_id identity PK │ FK → orders │ from_status (NULL = birth) │ to_status
    │                 event jsonb │ created_at │ idx (client_order_id,event_id) │ APPEND-ONLY
    └── triggers      orders_guard (entry state + 13 frozen edges, 23514, updated_at)
                      orders_append_event (1 audit row per INSERT/transition, §B snapshot)
                      order_events_block_mutation (UPDATE/DELETE/TRUNCATE raise P0001)
```

Live-apply runbook (init-scripts only auto-run on a fresh volume; the bind mount makes new
files visible immediately):

```bash
docker exec quant-postgres psql -U postgres -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/01_create_databases.sql
docker exec quant-postgres psql -U postgres -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/12_schema_execution.sql
# prove idempotency: re-run both — rc 0, only "already exists, skipping" NOTICEs
docker exec quant-postgres psql -U postgres -d db_execution -c '\dt execution.*'
```

## Implementation Steps

1. ✅ `quant-infra-db` on `main` → branch `feat/execution-schema`.
2. ✅ Add `db_execution` to `01_create_databases.sql`; author `12_schema_execution.sql`.
3. ✅ Add `execution_dsn` to `src/config.py`; `test_execution_dsn_format` unit test.
4. ✅ Author `tests/test_execution_schema.py` (14 infra tests, outer-tx-rollback +
   savepoint idiom; suite leaks zero rows).
5. ✅ Live-apply to running `quant-postgres` (twice — second run no-op).
6. ✅ `uv run pytest -m infra` (14/14 new tests pass; 3 pre-existing `test_mongo.py`
   failures reproduced on the base commit — unrelated Mongo auth issue).
7. ✅ Gate: ruff check/format, mypy strict, pytest (97 passed, 98% cov).
8. ✅ Update infra-db CHANGELOG / CLAUDE.md / docs/overview.md / docs/modules.md.
9. ✅ PR ① `lumduan/quant-infra-db#11` → merge to `main`.
10. ✅ This repo: plan doc + ROADMAP `[x]` + CLAUDE.md banner + knowledge notes → PR ②.
11. ✅ Umbrella: pin bumps (infra-db + this repo) + cross-cutting status flips → PR ③.

## File Changes

| File | Action | Description |
|---|---|---|
| `quant-infra-db/init-scripts/01_create_databases.sql` | Modified | `db_execution` `\gexec` guard |
| `quant-infra-db/init-scripts/12_schema_execution.sql` | Created | The order store (tables, CHECKs, triggers, indexes) |
| `quant-infra-db/src/config.py` | Modified | `execution_dsn` property |
| `quant-infra-db/tests/test_execution_schema.py` | Created | 14 live-DB infra tests |
| `quant-infra-db/tests/test_config.py` | Modified | `test_execution_dsn_format` |
| `quant-infra-db/CHANGELOG.md` / `CLAUDE.md` / `docs/{overview,modules}.md` | Modified | Schema reference + DB inventory |
| `docs/plans/phase1-execution-order-store.md` (this repo) | Created | This plan |
| `docs/plans/ROADMAP.md` (this repo) | Modified | Phase 1 `[ ]` → `[x]` + banner |
| `CLAUDE.md` (this repo) | Modified | Current-state banner → Phase 1 complete, next Phase 2 |
| `.claude/knowledge/order-state-machine.md` (this repo) | Modified | DB-enforcement note |
| `.claude/knowledge/normalized-order-contract.md` (this repo) | Modified | DB-encoding note |
| Umbrella `CLAUDE.md`, `plans/feature-execution-engine/ROADMAP.md`, `.claude/knowledge/{feature-execution-engine,optional-features-registry}.md` | Modified | Status flips + pin bumps (PR ③) |

## Success Criteria

- [x] `12_schema_execution.sql` applies idempotently against the live `quant-postgres` on
      `quant-network` (run twice; second run a no-op — verified manually AND by
      `test_schema_reapply_idempotent`).
- [x] Duplicate `client_order_id` rejected by the PK (`UniqueViolationError`).
- [x] Illegal transitions rejected (`PENDING_NEW → FILLED`, `NEW → REJECTED`,
      `PENDING_CANCEL → NEW`, `PENDING_REPLACE → CANCELLED`, terminal mutations — all
      raise 23514; no audit row appended).
- [x] Legal lifecycle `PENDING_NEW → NEW → PARTIALLY_FILLED → FILLED` succeeds and leaves
      exactly one `order_events` row per transition (4 rows incl. the birth row), with
      `broker_order_id` snapshotted on the ack row (§B).
- [x] `order_events` append-only holds (UPDATE/DELETE/TRUNCATE raise).
- [x] `db_execution` / `execution.*` reachable as `quant-postgres` from `quant-network`.
- [x] Enum/check constraints carry the full frozen sets; `numeric(18,6)` round-trips
      `Decimal` exactly.
- [x] infra-db gate green (ruff check + format, mypy strict, pytest 97 passed / 98% cov).
- [x] Plan doc exists with the prompt embedded; ROADMAP/CLAUDE/knowledge statuses updated.
- [x] PRs sequenced ① infra-db code → ② this repo docs → ③ umbrella pins+docs.

## Completion Notes

### Summary

Shipped exactly the frozen Phase 1 scope in `lumduan/quant-infra-db#11`
(`feat(execution): add execution order store schema (Phase 1)`). The schema was
live-applied to the running `quant-postgres` (applied twice — second run a pure no-op) and
verified by 14 new infra tests (all passing; the test suite leaks zero rows thanks to the
outer-transaction-rollback + savepoint idiom). The full infra-db quality gate is green.

### Notable realisation details

- The DB now enforces, with zero app code: the idempotency PK, the single entry state,
  exactly the 13 frozen transitions, terminal immutability, one immutable audit row per
  transition (birth row included), and at-least-once fill dedupe.
- The §B atomic ack is realised as "one UPDATE statement sets `status='NEW'` +
  `broker_order_id`; the AFTER trigger writes the audit row in the same transaction".

### Issues Encountered

- 3 pre-existing `tests/test_mongo.py` infra failures (Mongo auth) — reproduced on the
  base commit `fbde7c3`, unrelated to this phase; left for an infra-db housekeeping fix.

### Decisions Deferred (→ Phase 2/3)

- App-level transition guard + typed errors (Phase 2, over this schema).
- `execution_engine` DB role + least-privilege grants (Phase 2; documented in the script
  header).
- Frozen-graph amendments for venue cancel-rejects, fills-while-`PENDING_CANCEL`, and the
  Liberator cancel+replace end-state (`PENDING_REPLACE → CANCELLED`) — require an ADR
  amendment + follow-up migration before any adapter needs them.
- `reject_reason` / `metadata` columns (ALTER when Phase 2 needs them).
