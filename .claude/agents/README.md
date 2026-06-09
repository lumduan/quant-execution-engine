# Claude Agents

Specialized sub-agents for this project. Reference an agent in your prompt to
invoke its expertise.

## Available Agents

### Architecture & Code Quality
| Agent | Purpose |
|---|---|
| [`@python-architect`](python-architect.md) | Architecture, async patterns, type safety, code quality |
| [`@refactor-specialist`](refactor-specialist.md) | Behavior-preserving structural change under green tests |
| [`@api-designer`](api-designer.md) | REST / FastAPI design, schemas, versioning, OpenAPI quality |

### Engineering Workflow
| Agent | Purpose |
|---|---|
| [`@dependency-manager`](dependency-manager.md) | uv package management, dependency updates, environment setup |
| [`@git-commit-reviewer`](git-commit-reviewer.md) | Pre-commit validation, commit message standards, repo hygiene |
| [`@documentation-specialist`](documentation-specialist.md) | Docstrings, API docs, usage examples |
| [`@release-manager`](release-manager.md) | Version bumps, CHANGELOG, tagging, publish, smoke test |

### Reliability
| Agent | Purpose |
|---|---|
| [`@bug-investigator`](bug-investigator.md) | Root-cause analysis, repro-first fixes, regression tests |
| [`@test-engineer`](test-engineer.md) | pytest specialist — unit, integration, regression, property tests |
| [`@performance-optimizer`](performance-optimizer.md) | Profiling, latency, memory, hot-path optimization |
| [`@security-reviewer`](security-reviewer.md) | Secrets, injection, auth, validation, dep CVEs |

## Usage

Reference an agent in your prompt to invoke its expertise:

```
@python-architect review this new data processing module
@dependency-manager add httpx to the project dependencies
@git-commit-reviewer prepare a commit for the authentication changes
@documentation-specialist add docstrings to src/core.py
@bug-investigator reproduce the timeout error in the health endpoint
@test-engineer add edge-case coverage for the input parser
@performance-optimizer profile the batch processing loop
```

## Related

- [Project Skill](../knowledge/project-skill.md) — master rules file
- [Coding Standards](../knowledge/coding-standards.md) — enforceable conventions
- [Playbooks](../playbooks/) — step-by-step workflows
