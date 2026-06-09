# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial template scaffold: `src/`, `tests/`, `docs/`, `.claude/`, `.github/`.
- `pyproject.toml` with `ruff`, `mypy`, `pytest`, `pytest-asyncio`, `pytest-cov`, `bandit`, `pip-audit`.
- Multi-stage `Dockerfile` (uv-native, Python 3.11-slim).
- CI workflow (lint, format check, type check, test with coverage) on Python 3.11 and 3.12.
- Docker publish workflow targeting GHCR.
- Weekly security scan workflow (`bandit` + `pip-audit`).
- AI-agent enablement: `.claude/knowledge/project-skill.md`, `.claude/playbooks/feature-development.md`, `.claude/prompts/Prompt-Engineer.prompt.md`.
- Issue templates (bug, feature), PR template, `FUNDING.yml`.

[Unreleased]: https://github.com/OWNER/REPO/compare/HEAD...HEAD
