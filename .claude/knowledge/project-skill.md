# Project Skill — Operating Rules

Top-level rules every agent and contributor must follow when working in this
repository.

## Hard Rules

1. **Always `uv run`.** Never `python`, `pip`, `poetry`, or `conda` directly.
2. **Async-first I/O.** All HTTP via `httpx.AsyncClient`. `requests` is
   forbidden in library code.
3. **Pydantic at boundaries.** Function I/O between modules goes through
   Pydantic models — never raw dicts.
4. **Type hints everywhere.** Full annotations on all public functions — args
   and return. No bare `Any`.
5. **≥80% test coverage.** Enforced in CI. New features must include tests.
6. **No secrets in repo.** All config via env vars. `.env` is gitignored.
7. **Ruff + mypy clean before commit.** Run the full quality gate.
8. **Conventional Commits.** `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`.

## Soft Conventions

- File size target: ≤ 500 lines per Python file.
- Public functions: full type annotations + Google-style docstring.
- Logging: `logging.getLogger(__name__)` — never `print` in library code.
- Prefer stdlib over third-party dependencies where the stdlib module is
  adequate.

## Where to Look First

- Architecture: [architecture.md](architecture.md)
- Standards: [coding-standards.md](coding-standards.md)
- Commands: [commands.md](commands.md)
- Stack reasoning: [stack-decisions.md](stack-decisions.md)
- Known recurring bugs: [../memory/recurring-bugs.md](../memory/recurring-bugs.md)
- Anti-patterns to avoid: [../memory/anti-patterns.md](../memory/anti-patterns.md)
