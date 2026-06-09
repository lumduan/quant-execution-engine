# python-template

> Universal Python project template — uv-native, Docker-ready, AI-agent enabled.

[![CI](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/ci.yml)
[![Docker Publish](https://github.com/OWNER/REPO/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/docker-publish.yml)
[![Security Scan](https://github.com/OWNER/REPO/actions/workflows/security.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/security.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A fork-ready Python project template with dependency management via [uv](https://docs.astral.sh/uv/),
Docker support for containerized execution, CI/CD workflows, and a `.claude/`
directory that AI coding agents use for project context and standards.

## Features

- **uv-native** — single `pyproject.toml` as the source of truth.
- **Docker** — multi-stage build with `uv`, Python 3.11-slim, ready to deploy.
- **Type-safe** — `mypy --strict` on all source and test code.
- **Linted & formatted** — `ruff` with E, F, I, UP, B, SIM rules.
- **≥80% coverage** — `pytest` + `pytest-asyncio` + `pytest-cov` enforced in CI.
- **Security scanning** — weekly `bandit` and `pip-audit` runs.
- **Pre-commit hooks** — ruff-check, ruff-format, mypy on every commit.
- **AI agent ready** — `.claude/` directory with knowledge, playbooks, and prompt
  engineering guidance.

## Directory structure

```
.
├── .claude/                       # AI agent context & playbooks
│   ├── knowledge/project-skill.md # Master rules for all code
│   ├── playbooks/                 # Step-by-step workflow guides
│   └── prompts/                   # Prompt engineering instructions
├── .github/                       # CI/CD, issue/PR templates
│   ├── workflows/                 # ci.yml, docker-publish.yml, security.yml
│   ├── ISSUE_TEMPLATE/
│   ├── PULL_REQUEST_TEMPLATE.md
│   └── FUNDING.yml
├── src/                           # Application source
│   └── main.py                    # Entrypoint
├── tests/                         # Test suite
├── docs/                          # Documentation
├── Dockerfile                     # Multi-stage container build
├── pyproject.toml                 # uv project config + tool settings
├── uv.lock                        # Locked dependency versions
├── .pre-commit-config.yaml
├── .env.example
├── CHANGELOG.md
├── CODE_OF_CONDUCT.md
├── CONTRIBUTING.md
├── LICENSE
└── SECURITY.md
```

## Prerequisites

- Python 3.11 or 3.12
- [uv](https://docs.astral.sh/uv/) (install with `curl -LsSf https://astral.sh/uv/install.sh | sh`)

## Installation

```bash
git clone https://github.com/OWNER/REPO.git
cd REPO

# Install all dependencies (dev group included by default)
uv sync --all-groups

# Install pre-commit hooks
uv run pre-commit install
```

## Running locally

```bash
uv run python -m src.main
# Output: hello from python-template
```

## Running with Docker

```bash
# Build
docker build -t python-template:dev .

# Run
docker run --rm python-template:dev
# Output: hello from python-template
```

## Testing

```bash
# Run all tests
uv run pytest

# With verbosity and coverage
uv run pytest -v --cov=src --cov-report=term-missing
```

Coverage must stay ≥80%. The threshold is enforced in CI and in `pyproject.toml`
(`tool.pytest.ini_options.addopts`).

## Linting, formatting, and type checking

```bash
uv run ruff check .               # Lint
uv run ruff format --check .      # Format check (passive)
uv run ruff format .              # Auto-format (apply)
uv run mypy src tests             # Type check
```

Run all quality gates together:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest
```

## Using `.claude/` for AI agent workflows

This project is designed to work with AI coding agents like Claude Code.
The `.claude/` directory provides agents with project context and enforceable
standards:

| File | Purpose |
|------|---------|
| `.claude/knowledge/project-skill.md` | **Start here.** Hard rules, soft conventions, and quality gates. Agents load this first. |
| `.claude/playbooks/feature-development.md` | Repeatable 8-step workflow: read → design → test-first → implement → quality gate → document → commit → verify. |
| `.claude/prompts/Prompt-Engineer.prompt.md` | How to write effective prompts for AI agents on this project. Includes good and bad examples. |

When you open this repo in Claude Code (or any agent that reads `.claude/`),
the agent will automatically pick up these files. You can also ask it explicitly:
> *"Read `.claude/knowledge/project-skill.md` and then follow
> `.claude/playbooks/feature-development.md` to add a new feature."*

## Security scanning

```bash
# Static analysis for common Python security issues
uv run bandit -r src

# Check dependencies for known CVEs
uv run pip-audit
```

Both run automatically on a weekly CI schedule (`.github/workflows/security.yml`).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contribution guide,
conventional commit format, and quality gate expectations. Pull requests are
welcome — use the PR template to provide context.

## Security

Report vulnerabilities privately to **bad.sonsuk@gmail.com** rather than
opening a public issue. See [SECURITY.md](SECURITY.md) for the full policy.

## License

MIT — see [LICENSE](LICENSE) for details.
