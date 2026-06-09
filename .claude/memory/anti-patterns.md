# Anti-Patterns

Things to **avoid** in this repo. Each entry: the bad pattern → why → the right
way.

---

## `requests` in async code

- **Bad**: `import requests; r = requests.get(url)` inside an `async def`.
- **Why**: blocks the event loop; degrades throughput.
- **Right**: `async with httpx.AsyncClient(timeout=...) as c: r = await c.get(url)`.

---

## Mocking data structures in tests

- **Bad**: `mock.patch("pandas.DataFrame")` or `mock.MagicMock(spec=MyModel)`.
- **Why**: tests pass while production breaks on real shape, dtype, or
  validation issues.
- **Right**: use real objects with minimal valid data.

---

## Hidden global config inside modules

- **Bad**: `from config import SETTINGS; SETTINGS["key"] = "value"`.
- **Why**: action-at-a-distance; hides dependencies; breaks tests.
- **Right**: load settings once at startup, pass down explicitly or inject.

---

## Bare `except:` and `except Exception: pass`

- **Bad**: `try: x() except: pass`.
- **Why**: hides bugs, hides keyboard interrupt, makes debugging impossible.
- **Right**: catch the narrowest type, log + re-raise or convert to a domain
  exception.

---

## `print` in library code

- **Bad**: `print(f"got {n} rows")` left in committed code.
- **Why**: bypasses log levels, no structure, can't be filtered or routed.
- **Right**: `logger = logging.getLogger(__name__); logger.info("got %d rows", n)`.

---

## Hard-coded paths

- **Bad**: `df = pd.read_parquet("/Users/alice/data/file.parquet")`.
- **Why**: breaks on every other machine and in CI.
- **Right**: paths come from `Settings()` (env var with a project-relative
  default).

---

## Optimizing without a benchmark

- **Bad**: rewriting code for "speed" because it "looks slow".
- **Why**: spends time, increases complexity, often achieves nothing or
  regresses.
- **Right**: profile first; capture before/after numbers; commit the benchmark.

---

## Refactor + feature in one PR

- **Bad**: rename + restructure + add new endpoint, all in one commit.
- **Why**: review becomes impossible; rollback is all-or-nothing.
- **Right**: refactor PR (no behavior change), then feature PR.

---

## No type annotations on public functions

- **Bad**: `def process(data): ...`.
- **Why**: no IDE support, no mypy checking, ambiguous contract.
- **Right**: `def process(data: list[Item]) -> list[Result]: ...`.

---

## Returning bare `dict` from public functions

- **Bad**: `def get_config() -> dict: return {"key": "val"}`.
- **Why**: no schema validation, easy to drift between versions, no IDE
  autocomplete.
- **Right**: return a typed model (Pydantic, dataclass, TypedDict).

---

> **Append new anti-patterns as you discover them. Pattern → Why → Right way.**
