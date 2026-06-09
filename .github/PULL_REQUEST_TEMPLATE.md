## Summary

<!-- Briefly describe what this PR does. -->

## Changes

<!-- List the key changes with file paths. -->

- ...

## Test plan

<!-- Check the boxes after verifying each step. -->

- [ ] `uv run ruff check .` passes
- [ ] `uv run ruff format --check .` passes
- [ ] `uv run mypy src tests` passes
- [ ] `uv run pytest` passes (≥80% coverage)
- [ ] `docker build -t python-template:dev .` succeeds
- [ ] Manual smoke test: `docker run --rm python-template:dev` works as expected

## Risk & rollback

<!-- Any deployment risks? How would we revert? -->

## Related

<!-- Link to issues, discussions, or design docs. -->

Closes #
