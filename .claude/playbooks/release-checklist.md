# Playbook — Release Checklist

Owned by [agents/release-manager.md](../agents/release-manager.md). Every step
gated; no skipping.

## 1. Pre-flight

- [ ] Working tree clean: `git status` shows nothing.
- [ ] On the correct branch (`main`).
- [ ] `uv sync` is clean — no drift in `uv.lock`.

## 2. Quality Gate (must be 100% green)

- [ ] `uv run pytest -v`
- [ ] `uv run pytest --cov=src --cov-report=term-missing` — coverage ≥ 80%.
- [ ] `uv run mypy src tests` — clean.
- [ ] `uv run ruff check .` — clean.
- [ ] `uv run ruff format --check .` — clean.

## 3. Version Bump

- [ ] Edit `pyproject.toml` `[project] version` per SemVer:
  - **MAJOR** for breaking changes.
  - **MINOR** for backward-compatible features.
  - **PATCH** for backward-compatible fixes only.
- [ ] Run `uv lock` to refresh lockfile metadata.

## 4. CHANGELOG

- [ ] Add a new section `## [X.Y.Z] — YYYY-MM-DD`.
- [ ] Subsections: `Added`, `Changed`, `Fixed`, `Removed`, `Security`.
- [ ] Entries describe **user-visible** impact, not internal churn.
- [ ] Reference any breaking change with a "Migration" note.

## 5. Commit & Tag

- [ ] Commit version bump + CHANGELOG together: `chore(release): vX.Y.Z`.
- [ ] Tag locally: `git tag vX.Y.Z`.
- [ ] Push: `git push && git push --tags`.

## 6. Docker Smoke Test

- [ ] `docker build -t <name>:vX.Y.Z .`
- [ ] `docker run --rm <name>:vX.Y.Z` — expected output.
- [ ] If applicable, health endpoint responds.

## 7. Announce

- [ ] Post release notes (CHANGELOG section) wherever the team consumes them.
- [ ] Close any milestone tracking the release.

## 8. Rollback Plan

- [ ] If critical regression: open a hotfix branch from the previous tag.
- [ ] Cut a `X.Y.Z+1` patch following this checklist.
