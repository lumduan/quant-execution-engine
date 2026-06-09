# <type>(<scope>): <short imperative title>

> Conventional commit types: feat, fix, refactor, perf, test, docs, chore,
> build, ci.

## Summary
_2-3 sentence overview. What changed and why. User impact in one line._

## Changes
- `src/<module>.py` — <one-line description>
- `tests/<path>.py` — <added regression / coverage for X>
- `docs/...` — <updated section X>

## Technical Implementation
_For non-trivial changes: how the change is implemented, key design decisions,
trade-offs. Skip if the diff is self-explanatory._

## Test Plan
- [ ] `uv run pytest -v` — full suite passes.
- [ ] `uv run mypy src tests` — clean.
- [ ] `uv run ruff check . && uv run ruff format --check .` — clean.
- [ ] `uv run pytest --cov=src --cov-report=term-missing` — coverage ≥ 80%.
- [ ] Manual verification: <script run, curl, browser click-through>.
- [ ] `docker build -t <name>:dev . && docker run --rm <name>:dev` — works as
  expected.

## Risk & Rollback
- **Risk level**: low / medium / high.
- **Blast radius**: _which modules / users / endpoints can be affected if this
  is wrong_.
- **Rollback**: revert this PR (`git revert <sha>`).

## Docs / Changelog
- [ ] CHANGELOG entry added (if user-visible).
- [ ] Public docstrings updated.
- [ ] Example added or updated (if user-facing API).

## Related
- Closes #<issue>
- Plan: `docs/plans/<file>.md`
- Memory: `.claude/memory/recurring-bugs.md` (if a regression class)

---
Co-authored-by: Claude
