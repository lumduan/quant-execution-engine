# AI Agent Context

## Project Overview

A Python project built with modern tooling: uv for dependency management, async-first I/O, type-safe with mypy strict mode, and Docker-ready.

## Core Purpose

This project provides a type-safe, async-first Python codebase following modern best practices for reliability, testability, and maintainability.

## Architecture & Tech Stack

### Core Framework

- **Python 3.11+**: Modern Python with full type hint support.
- **Pydantic V2**: Data validation and settings management with strict type enforcement.
- **Async/Await**: Non-blocking I/O operations for optimal performance.

### Dependencies & Package Management

- All Python dependencies MUST be managed using [uv](https://github.com/astral-sh/uv).
- Add dependencies with `uv add <package>` or `uv add --dev <package>`.
- Remove dependencies with `uv remove <package>`.
- Lock dependencies with `uv lock`; `uv.lock` is always committed.
- Run Python scripts and modules using `uv run python <script.py>` or `uv run python -m <module>`.
- Do NOT use pip, poetry, or conda for dependency management or Python execution.

### Design Principles

- **Type Safety First**: Every function has complete type annotations.
- **Async by Default**: All I/O operations use async/await.
- **Pydantic at Boundaries**: Data crossing module boundaries uses Pydantic models.
- **Test Coverage ≥ 80%**: Enforced in CI.
- **uv-Only Workflow**: Every command prefixed with `uv run`.

## Project Structure

```
.
├── src/                  # Core library — importable Python package
│   └── main.py           # Entrypoint
├── tests/                # pytest suite mirroring src/ structure
├── docs/                 # Documentation and design docs
├── examples/             # Runnable usage examples
├── scripts/              # Utility scripts
├── .claude/              # AI agent context, playbooks, and knowledge
├── .github/              # CI/CD workflows, issue/PR templates, AI instructions
├── pyproject.toml        # Project configuration and dependencies
├── uv.lock               # Locked dependencies
├── Dockerfile            # Multi-stage container build
└── README.md             # Project documentation
```

## Environment Configuration

### Required Environment Variables

Copy `.env.example` to `.env` and fill in real values. Never commit `.env`.

## Core Modules

Document your core modules here as the project grows. Each module should have:

- A clear single responsibility.
- Public API with full type annotations and docstrings.
- Corresponding tests in `tests/`.

## Key Conventions

- **Quality gate before every commit**: `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest`
- **Conventional Commits**: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`
- **No secrets in code**: All config via environment variables.
- **No `print` in library code**: Use `logging.getLogger(__name__)`.

## AI Agent Workflow

See `.claude/knowledge/project-skill.md` for the master rules file that AI agents load first.
See `.claude/playbooks/feature-development.md` for the step-by-step development workflow.
