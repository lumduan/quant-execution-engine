# Commands

Canonical commands. Every Python invocation is prefixed with `uv run`.

## Environment

| Task | Command |
|---|---|
| Sync deps from lockfile | `uv sync` |
| Add a runtime dep | `uv add <pkg>` |
| Add a dev dep | `uv add --dev <pkg>` |
| Add to a group | `uv add --group <group> <pkg>` |
| Remove a dep | `uv remove <pkg>` |
| Upgrade one package | `uv lock --upgrade-package <pkg> && uv sync` |
| Upgrade all packages | `uv lock --upgrade && uv sync` |
| Show tree | `uv tree` |
| Audit (CVE) | `uv pip audit` |

## Quality Gate

| Task | Command |
|---|---|
| Tests | `uv run pytest -v` |
| Tests + coverage | `uv run pytest --cov=src --cov-report=term-missing` |
| Single test | `uv run pytest tests/<path>::<test_name> -v` |
| Type check | `uv run mypy src tests` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Format check | `uv run ruff format --check .` |
| Pre-commit run | `uv run pre-commit run --all-files` |

## Run

| Task | Command |
|---|---|
| Main entrypoint | `uv run python -m src.main` |
| Run a script | `uv run python scripts/<name>.py` |
| Run an example | `uv run python examples/<name>.py` |

## Docker

| Task | Command |
|---|---|
| Build | `docker build -t <name>:dev .` |
| Run | `docker run --rm <name>:dev` |
| Shell into image | `docker run --rm -it <name>:dev /bin/bash` |

## Profiling

| Task | Command |
|---|---|
| cProfile a script | `uv run python -m cProfile -o profile.out scripts/<name>.py` |
| py-spy live | `uv run py-spy top -- python scripts/<name>.py` |
| memray | `uv run memray run scripts/<name>.py && uv run memray flamegraph memray-*.bin` |

## Security

| Task | Command |
|---|---|
| Bandit static analysis | `uv run bandit -r src` |
| Dependency audit | `uv run pip-audit` |

## Quick combined gates

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest -v
```
