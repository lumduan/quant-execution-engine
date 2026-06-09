# Playbook — Code Review

For reviewing PRs / diffs. Read tests first; they document intent.

## 1. Skim Scope

- One logical change? If a PR mixes refactor + feature, ask for a split.
- Does the diff stay within the layer it claims to touch?

## 2. Read Tests First

- Do the tests describe the **behavior** the feature claims?
- Are edge cases covered: empty input, None, out-of-range, error paths?
- Is there a regression test for any bug fixed?
- No mocked data structures; no real network in unit tests.

## 3. Read Code

- Standards check against [knowledge/coding-standards.md](../knowledge/coding-standards.md):
  - Full type annotations
  - Pydantic at boundaries
  - Logging not `print`
  - Module-specific exceptions, not bare `Exception`
  - File size budget respected
- Cross-cutting suspects (auto-flag):
  - `requests` in async path → block.
  - Hard-coded paths or secrets → block.
  - Bare `except:` → block.
  - Missing input validation at boundaries → block.

## 4. Security Pass

- New external surface (HTTP route, CLI, file I/O on user input)?
- Missing auth on a non-public endpoint?
- Input validation present?
- Errors leaking internals to clients?

## 5. Performance Pass

- New I/O — batched, timed out, retried?
- Data processing — streaming or vectorized where appropriate?
- Large allocations — any obvious memory issues?

## 6. Docs

- Public functions have docstrings (Google style).
- CHANGELOG updated if user-visible.
- `docs/` updated if architectural.
- Examples updated or added if new user-facing API.

## 7. Decide

- **Approve** if all blocks resolved.
- **Request changes** with concrete `file:line` references and a fix per
  finding.
- **Comment** for non-blocking suggestions, clearly labeled "non-blocking".

## 8. Don't

- Don't approve without reading tests.
- Don't approve a refactor that mixes in a feature.
- Don't nitpick formatting — ruff handles that.
- Don't ask for stylistic preferences as blocking changes.
