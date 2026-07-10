# Contributing to HealthClaw Guardrails

Thanks for being here. This project is an **open reference implementation** of the
security and compliance layer between AI agents and clinical data — the guardrails
that sit between [FHIR](https://hl7.org/fhir/) and [MCP](https://modelcontextprotocol.io/).
It is a community effort, not a commercial product. It only becomes trustworthy if
people with different vantage points pressure-test it.

**You don't have to write code to contribute.** A well-argued "you got the SDC
extraction semantics wrong" issue is as valuable as a PR.

## Who we'd especially love to hear from

- **Implementers** building FHIR × MCP integrations — where do these patterns break in the real world?
- **Clinicians & compliance/privacy folks** — challenge the redaction profiles, the audit model, and the documented HIPAA postures (see [`.claude/compliance/hipaa.md`](.claude/compliance/hipaa.md)).
- **Standards people** (HL7, SDC, SMART on FHIR, US Core) — tell us where we've diverged from the spec, especially on `$populate` / `$extract` and the R6 ballot resources.
- **Anyone** — open an issue, file a correction, suggest a doc fix, or send a PR.

## Ground rules

- Be kind and assume good faith. See the [Code of Conduct](CODE_OF_CONDUCT.md).
- **Every PR gets an automated standards review** within minutes (against
  [.github/REVIEW_STANDARDS.md](.github/REVIEW_STANDARDS.md)) and merges
  automatically once CI is green, the review passes, and a maintainer
  approves. You'll always get specific, actionable feedback — never a silent
  rejection.
- **No CLA — DCO instead.** Sign off your commits (`git commit -s`, Developer Certificate of Origin). Contributions are accepted under the project's [MIT license](LICENSE); see [LICENSING.md](LICENSING.md) for the licensing posture going forward.
- **Never commit secrets or real PHI.** This repo uses synthetic demo data only. PHI must never appear in code, tests, fixtures, logs, or commit history.

## Project shape (orient yourself fast)

- **Flask app (Python)** — the FHIR REST facade + guardrail stack, under `r6/`. Entry point `main.py`.
- **MCP server (Node/TypeScript)** — `services/agent-orchestrator/`.
- **The guardrail rules that matter** — build/test commands, architecture map, and the security invariants — live in [docs/development.md](docs/development.md). Read it first; it's the fastest way to learn the non-obvious parts.

## Development setup

```bash
# Python (3.11+; CI runs 3.11)
uv sync
STEP_UP_SECRET=dev-secret python main.py        # Flask on :5000

# MCP server
cd services/agent-orchestrator && npm ci && npm start

# Full stack
docker-compose up -d --build
```

## Running the tests

Every change should keep the suite green.

```bash
# Python (700+ tests)
uv run python -m pytest tests/ -v
uv run python -m pytest tests/test_sdc_routes.py -v        # one file
uv run python -m pytest tests/test_sdc_routes.py::test_name -v  # one test

# MCP server
cd services/agent-orchestrator && npx tsc --noEmit && npm test

# End-to-end (requires Flask on :5000)
cd e2e && npm ci && npx playwright install --with-deps chromium && npm test
```

We follow **test-driven development**: write the failing test first, then the
minimal code to pass it. PRs that add behavior without tests will be asked for tests.

## The bar for a change touching PHI, audit, redaction, or access control

These are the load-bearing parts. Before changing any of them, read
[`.claude/compliance/hipaa.md`](.claude/compliance/hipaa.md) and keep these invariants:

- **Every FHIR resource access emits an `AuditEvent`** in the same transaction.
- **Writes require a step-up token**; clinical writes additionally require human-in-the-loop confirmation (`X-Human-Confirmed`), except ingest-class bundle operations which are documented as exempt.
- **Reads of non-public tenants are authenticated**, not just tenant-scoped.
- **No PHI in logs or audit `detail`** — counts, types, and tenant IDs only.
- `validate_step_up_token` returns a `(bool, str)` tuple — **destructure both**; never coerce the tuple to a boolean (a non-empty tuple is truthy → silent auth bypass).

CI enforces a subset of these automatically in the `compliance-gates` job. If your
change weakens a gate, the build fails — that's intentional.

## Submitting a pull request

1. Fork and branch from `main` (e.g. `fix/...`, `feat/...`, `docs/...`).
2. Make focused commits; keep the diff scoped to one concern.
3. Run the full test suite locally and confirm it's green.
4. Open a PR describing **what** changed and **why**, and how you verified it.
5. A maintainer reviews for correctness, the guardrail invariants above, and spec conformance.

For larger features, open an issue first so we can align on the design — for
substantial work we keep a short design doc under `docs/design/`.

## Reporting security issues

Please **do not** open a public issue for a vulnerability. Follow the process in
[SECURITY.md](SECURITY.md).

## Questions

Open a GitHub issue, or reach the maintainers via the channels listed on
[healthclaw.io](https://healthclaw.io). We'd rather you ask than guess.
