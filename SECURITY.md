# Security Policy

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 0.x     | :white_check_mark: |

## Reporting a vulnerability

**Do not open a public issue.** Instead, send details privately to
**bad.sonsuk@gmail.com**.

Please include:

- A description of the vulnerability
- Steps to reproduce
- Affected versions (or commit hash)
- Any suggested mitigations

You will receive an acknowledgment within 48 hours and a status update within
7 days. Once resolved, we will coordinate disclosure timing with you.

## Security tooling

This project runs automated scans:

- **Bandit** — static analysis for common Python security issues (weekly CI).
- **pip-audit** — checks dependencies for known CVEs (weekly CI).

To run locally:

```bash
uv run bandit -r src
uv run pip-audit
```
