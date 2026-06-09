# Stack Decisions

Why each tool was chosen. One-liner per decision; rationale captures the
trade-off.

## Package & Runtime

- **uv** — fastest resolver, deterministic locks, single binary. Replaces pip /
  poetry / conda. Trade-off: newer than poetry, smaller community knowledge base.
- **Python 3.11+** — required for typing improvements (`Self`, better generics)
  and asyncio performance gains. Trade-off: forecloses use on older infra.

## Web / API (when applicable)

- **FastAPI** — async-native, OpenAPI for free, Pydantic-native, mature.
  Trade-off: opinionated about Pydantic versions; we accept that as a feature.
- **uvicorn** — lightweight ASGI server, official FastAPI pairing.

## Data (when applicable)

- **pandas + PyArrow / Parquet** — columnar storage for structured data,
  zero-copy interop, fast partitioned reads.
- **numpy** — numeric foundation under pandas.

## Validation / Config

- **pydantic v2** — speed + ergonomics for data validation.
- **pydantic-settings** — env-driven config; no hidden globals.

## HTTP

- **httpx** — async HTTP everywhere. `requests` is forbidden in library code
  (sync, blocks the event loop).

## Quality Tooling

- **pytest + pytest-asyncio** — standard, mature, async-native.
- **mypy** — strict type checking on `src/`.
- **ruff** — single tool replaces flake8 + isort + black; fast.
- **pre-commit** — local quality gate before commit.
- **bandit** — static analysis for common Python security issues.
- **pip-audit** — dependency CVE scanning.

## Containerization

- **Docker** — multi-stage builds, reproducible runtime, CI/CD ready.
- **Python slim images** — small attack surface, fast pulls.

## What We Deliberately Don't Use

- `requests` — sync; replaced by `httpx`.
- `poetry` / `pip-tools` — replaced by `uv`.
- `conda` / `mamba` — replaced by `uv`.
- `flake8` / `isort` / `black` — replaced by `ruff`.

---

> Update this document when adding or removing a significant dependency.
> Keep entries short — name, one-line reason, one-line trade-off.
