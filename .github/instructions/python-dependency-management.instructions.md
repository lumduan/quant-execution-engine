---
applyTo: '**'
---
**Dependency Management & Python Execution:**

- All Python dependencies MUST be managed using [uv](https://github.com/astral-sh/uv).
- Add runtime dependencies with `uv add <package>`.
- Add dev dependencies with `uv add --dev <package>`.
- Add group dependencies with `uv add --group <group> <package>`.
- Remove dependencies with `uv remove <package>`.
- Sync environment: `uv sync --all-groups`.
- Upgrade a package: `uv lock --upgrade-package <pkg> && uv sync`.
- Upgrade all packages: `uv lock --upgrade && uv sync`.
- Run Python scripts and modules using `uv run python <script.py>` or `uv run python -m <module>`.
- Do NOT use pip, poetry, or conda for dependency management or Python execution.
- Always commit `pyproject.toml` and `uv.lock` together.

**Dependency Hygiene:**

- Prefer stdlib over third-party where adequate.
- Question every new dependency: is the import worth the supply-chain risk?
- Run `uv pip audit` to check for known CVEs before merging.
- Pin to exact versions in `pyproject.toml` only with a documented reason.
