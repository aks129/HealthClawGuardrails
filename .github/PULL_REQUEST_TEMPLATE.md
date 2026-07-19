## What & why

<!-- One or two sentences. Link the issue if one exists. -->

## Checklist

- [ ] Tests added/updated and `uv run python -m pytest tests/ -q` passes
- [ ] `pipx run ruff check .` passes
- [ ] No PHI in code, tests, fixtures, or logs (synthetic data only)
- [ ] Touches auth/audit/redaction/tenancy? Note it here so maintainers review against the compliance rules
- [ ] Node changes: `npx tsc --noEmit && npm test` passes in `services/agent-orchestrator`
