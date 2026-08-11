## What & why

<!-- One or two sentences. Link the issue if one exists. -->

## Checklist

- [ ] Tests added/updated and `uv run python -m pytest tests/ -q` passes
- [ ] `uv run ruff check .` passes (not `uvx`/`pipx` — those fetch an unpinned ruff)
- [ ] No PHI in code, tests, fixtures, or logs (synthetic data only)
- [ ] Touches auth/audit/redaction/tenancy? Note it here so maintainers review against the compliance rules
- [ ] Node changes: `npx tsc --noEmit && npm test` passes in `services/agent-orchestrator`
- [ ] Read against [docs/defect-catalogue.md](../docs/defect-catalogue.md) — no known shape reintroduced
- [ ] New guards mutation-tested, with the result stated below (not just "tests pass")
- [ ] Anything deliberately left undone is named here, not omitted

## Mutations run

<!-- One line each: the edit you made, and whether the guard went red.
     "GREEN, and correctly so — no observable behaviour differs" is a fine
     answer. Silence is not. Delete this section only if the PR adds no
     guard, test or assertion. -->
