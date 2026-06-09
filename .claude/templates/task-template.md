# Task: <short imperative title>

## Goal
_One sentence stating the user-visible outcome. Not the implementation._

## Context
_Why this task exists. Link any related plan, prior PR, or memory entry. Note
any constraints (deadline, dependency, freeze)._

## Acceptance Criteria
- [ ] <Observable behavior 1>
- [ ] <Observable behavior 2>
- [ ] New / updated test passes: `uv run pytest tests/<path> -v`
- [ ] Quality gate clean:
  `uv run ruff check . && uv run mypy src tests && uv run pytest`
- [ ] Docstring + example added or updated where public API changed

## Out of Scope
- _Explicitly listed; if it's not here and not in acceptance criteria, ask
  before doing it._

## Verification Commands
```bash
# Run the relevant tests
uv run pytest tests/<path> -v

# Full quality gate
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest -v

# Manual smoke test (if applicable)
uv run python examples/<related_example>.py
```

## Files Likely Touched
- `src/<module>.py`
- `tests/<mirrored_path>.py`
- `docs/...` (if user-visible)

## Notes / Open Questions
- _Anything the implementer needs to clarify before starting._
