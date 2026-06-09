# Coding Standards

Concrete, enforceable rules. If you can't comply, document why in code with a
comment.

## Naming

- **Modules / functions / variables**: `snake_case`.
- **Classes / Pydantic models / TypedDicts**: `PascalCase`.
- **Constants / sentinels**: `SCREAMING_SNAKE_CASE`.
- **Private**: `_leading_underscore` for module-private.
- **Avoid abbreviations** except well-established domain terms.

## Typing

- Full type annotations on every function — args and return.
- No bare `Any`. If unavoidable, justify in a comment.
- Prefer `Sequence`, `Mapping`, `Iterable` for parameters; `list`, `dict` for
  returns when concrete.
- Use `Optional[X]` only when `None` is meaningful.
- Pydantic models for all data crossing module / process boundaries.

## File Size & Complexity

- Target ≤ 500 lines per `.py` file.
- Functions ≤ ~50 lines unless cohesion demands more.
- Split files when they exceed the budget; group related modules in packages.

## Errors

- Define module-local exceptions in each subpackage's `errors.py`.
- Inherit from a single root exception class.
- Never `raise Exception(...)`; never `except Exception: pass`.
- Catch the narrowest type that captures the failure mode.

## Imports

- ruff-isort sorted: stdlib → third-party → local, blank line between groups.
- No relative imports beyond one level (`from . import x`, never
  `from ...util import y`).
- No wildcard imports (`from x import *`).

## Logging

- `logger = logging.getLogger(__name__)` at module top.
- Never `print` in library code.
- Use `%` formatting for deferred interpolation:
  `logger.info("processed %d items", n)` — not f-strings.
- Never log secrets, tokens, or full request bodies.

## Async

- Every public function performing I/O is `async def`.
- Use `httpx.AsyncClient` (not `requests`) for HTTP.
- Use `asyncio.gather` for independent awaitables.
- Always set `timeout=` on HTTP calls.
- Use `async with` for resource management.

## Docstrings

Google style, mandatory on public functions:

```python
async def process_items(
    items: list[Item],
    *,
    batch_size: int = 100,
) -> list[Result]:
    """Process items in batches.

    Args:
        items: Input items to process. Must not be empty.
        batch_size: Number of items per batch. Defaults to 100.

    Returns:
        Processed results in the same order as input.

    Raises:
        ValueError: If `items` is empty.

    Example:
        >>> results = await process_items(items, batch_size=50)
    """
```

## Tests

- One test file per source file, mirroring path.
- `@pytest.mark.asyncio` for async tests.
- No network in unit tests; integration tests behind markers.
- Real data structures (no mocks of data types).
- See [Test Engineer](../agents/test-engineer.md).
