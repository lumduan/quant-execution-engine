---
applyTo: '**'
---
## File Organization — Strict Rules

### Directory Structure Requirements:

- `src/`: Core library — importable Python package.
- `tests/`: ALL pytest tests, comprehensive coverage required (≥80%).
- `examples/`: ONLY real-world usage examples, fully functional.
- `docs/`: ALL documentation and design docs.
- `scripts/`: Utility scripts for development and CI/CD.
- `.claude/`: AI agent context, knowledge, playbooks, and templates.

### File Naming Conventions:

- `snake_case` for all Python files.
- Clear, descriptive names indicating purpose.
- Test files MUST match pattern `test_*.py`.
- Example files SHOULD be descriptively named.

### Module Organization:

- One class/concern per module where practical.
- Group related modules in packages with `__init__.py`.
- Keep files under ~500 lines; split when exceeded.
- Mirror the source structure in the tests directory.

### File Deletion:

- Use `rm <path>` to delete files from the filesystem.
- Always confirm file removal before executing.
- Verify no remaining imports reference the deleted file.
