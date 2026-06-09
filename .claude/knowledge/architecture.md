# Architecture

Python 3.11+, async-first, type-safe, uv-managed. This document describes the
project's module boundaries, data flow, and structural conventions.

## Top-Level Layout

| Path | Purpose |
|---|---|
| `src/` | Core library — importable Python package. |
| `tests/` | pytest suite mirroring `src/` structure. |
| `docs/` | Design docs, plans, architecture decisions. |
| `examples/` | Runnable usage examples. |
| `.claude/` | Agent, knowledge, memory, playbook, and template config. |
| `.github/` | CI/CD workflows, issue/PR templates. |

## Module Boundaries (data flow)

```
External I/O  →  src/data        (fetch, normalize, persist)
              →  src/core        (business logic, computation)
              →  src/api         (expose results over HTTP, if applicable)
              →  src/cli         (command-line entrypoints, if applicable)
```

Direction is one-way: lower layers must not import from higher ones. Application
entrypoints (`api/`, `cli/`, `main.py`) may import `src/`; `src/` modules must
not import from entrypoint layers.

## Storage

- Choose the right storage for the access pattern:
  - **Columnar / time-series** → Parquet (via PyArrow).
  - **Key-value / metadata** → SQLite or JSON.
  - **Documents / config** → JSON or YAML.
- Partition large datasets by date or key where read patterns benefit.
- Document the storage choice in `docs/` with the rationale.

## Configuration

- All runtime config via environment variables (or `.env` for local dev).
- Use `pydantic-settings` for validated, typed settings.
- No hard-coded paths — base paths come from a single `Settings` object.
- Sensible defaults for local development; override in production via env.

## Cross-Cutting Conventions

- All I/O is async at boundaries; sync internal compute is fine.
- Errors: module-specific exceptions defined in each subpackage's `errors.py`,
  inheriting from a single root exception.
- Logging: `logging.getLogger(__name__)`; no `print` in library code.
- Time zone: always store as UTC; localize at presentation boundaries only.
