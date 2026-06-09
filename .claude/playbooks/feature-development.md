# Feature Development Playbook

Repeatable step-by-step workflow for adding new features.

## 1. Read context

- Read `project-skill.md` in `.claude/knowledge/`.
- Read related source files to understand existing patterns.
- Check `CHANGELOG.md` for recent changes that may conflict.

## 2. Design

- Sketch the implementation approach in plain text.
- Identify which files change and in what order.
- For non-trivial features, write a brief plan before coding.

## 3. Test first

- Write a failing test that defines the expected behavior.
- Keep tests small: one assert per test where practical.

## 4. Implement

- Write the minimum code to make the test pass.
- Follow the Hard Rules in `project-skill.md`.

## 5. Quality gate

All four must pass:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest
```

Fix every issue before moving on.

## 6. Document

- Add an `## [Unreleased]` entry to `CHANGELOG.md`.
- Update `README.md` if the feature is user-facing.
- Add or update docstrings on new public APIs.

## 7. Commit

Use a [Conventional Commits](https://www.conventionalcommits.org/) message:

```
feat: add <feature description>
```

Include `Co-Authored-By: Claude Code <noreply@anthropic.com>` if an AI agent
wrote the change.

## 8. Verify in Docker

```bash
docker build -t python-template:dev .
docker run --rm python-template:dev
```

Ensure the container starts cleanly and produces the expected output.
