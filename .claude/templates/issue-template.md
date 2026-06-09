# Issue Templates

Two flavors. Pick one and delete the other.

---

## Bug

### Title
`bug(<scope>): <short imperative description>`

### Reproduction
Smallest steps to reproduce. Prefer a code snippet or shell command runnable on
a fresh checkout.

```bash
uv sync
uv run python -c "<minimal repro>"
```

### Expected
_What should have happened._

### Actual
_What actually happened. Include the full traceback if any._

### Environment
- Git SHA: `git rev-parse HEAD`
- Python: `uv run python --version`
- OS: <macOS / Ubuntu / Windows>
- Key deps: `uv tree | grep -E "<relevant packages>"`

### Logs / Artifacts
```
<paste relevant log lines, redact any secrets>
```

### Related
- Memory: any recurring bug class? Link to `.claude/memory/recurring-bugs.md`
  section if so.
- Recent commits: `git log -p -S<symbol> -- src/<file>.py`.

---

## Feature

### Title
`feat(<scope>): <short imperative description>`

### Problem
_What user-visible problem are we solving? Who feels it?_

### Proposal
_What should the user be able to do? Describe the behavior, not the
implementation. Include shape of inputs and outputs if it's a new API._

### Alternatives Considered
- <Option A — why not chosen>
- <Option B — why not chosen>

### Acceptance Criteria
- [ ] <Observable behavior 1>
- [ ] <Observable behavior 2>
- [ ] Tests cover happy path + edge cases.
- [ ] Public API has docstring + example.
- [ ] CHANGELOG entry.

### Out of Scope
- _Explicit non-goals._

### Stakeholders
- Requested by: <name / role>
- Affected modules: <`src/...`>
