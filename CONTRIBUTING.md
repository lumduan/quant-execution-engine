# Contributing

Thanks for contributing. This document covers the workflow, conventions, and
quality gates for this project.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for dependency and environment management

```bash
# Clone and set up
git clone https://github.com/OWNER/REPO.git
cd REPO
uv sync --all-groups
uv run pre-commit install
```

## Quality gates

Every change must pass these checks before it can be merged:

```bash
uv run ruff check .              # Lint
uv run ruff format --check .     # Format check
uv run mypy src tests             # Type check
uv run pytest                     # Tests (≥80% coverage)
```

Run them all together:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest
```

## Code style

- **Python 3.11+** — use modern syntax where it improves clarity.
- **Type hints** on all public functions and methods.
- **Async by default** for I/O-bound work.
- **No secrets in code** — use environment variables or a `.env` file.
- **Ruff** enforces formatting and linting; no additional formatter needed.

## Pull request process

1. Create a feature branch from `main`.
2. Follow the feature development playbook at `.claude/playbooks/feature-development.md`.
3. Write tests for your change; coverage must stay ≥80%.
4. Update `CHANGELOG.md` under `## [Unreleased]`.
5. Open a PR using the pull request template.
6. All CI checks must pass before merge.

## Commit conventions

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add user authentication
fix: resolve race condition in task scheduler
docs: update README installation steps
chore: bump ruff to 0.7
```

## AI agent workflows

This project includes a `.claude/` directory that AI coding agents (e.g. Claude
Code) use for context and standards. See `.claude/knowledge/project-skill.md`
for the master rules file and `.claude/playbooks/` for step-by-step guides.

When working with an AI agent, it will automatically reference these files to
align with project conventions.
