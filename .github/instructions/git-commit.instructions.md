---
applyTo: '**'
---
### Commit Message Instructions

1. **Use Conventional Commits format**: `type(scope): summary`
2. **Keep summary ≤ 72 characters**, imperative mood.
3. **Body explains WHY**, not what (the diff shows what).
4. **List key files changed** with brief descriptions.
5. **Note testing or validation performed**.
6. **Reference related issues or PRs**.

### Commit Types

- `feat`: New feature
- `fix`: Bug fix
- `refactor`: Code restructure without behavior change
- `perf`: Performance improvement
- `test`: Test additions or changes
- `docs`: Documentation only
- `chore`: Maintenance, deps, config
- `build`: Build system changes
- `ci`: CI/CD changes

### Prohibited Actions

1. **NEVER** use bare `except:` clauses
2. **NEVER** ignore type checker warnings without justification
3. **NEVER** hardcode credentials or secrets
4. **NEVER** commit debug print statements
5. **NEVER** break existing public APIs without deprecation
6. **NEVER** add dependencies without updating `pyproject.toml`
7. **NEVER** commit code that doesn't pass all tests
8. **NEVER** use synchronous I/O for external API calls in library code
9. **NEVER** commit `.env` files or secrets
10. **NEVER** mix refactor and feature in a single commit
