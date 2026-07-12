# HealthClaw PR Review Standards

The checklist every PR is reviewed against — by humans and by the automated
reviewer. A PR merges only when all applicable items hold. (This file is the
public, self-contained distillation of the project's engineering rules; the
review bot reads THIS file, so keep it current.)

## Security & compliance (hard gates — any violation is REQUEST_CHANGES)

1. **No PHI in logs or audit detail.** Logger calls and `record_audit_event`
   detail strings must never contain names, identifiers, phone numbers,
   free-text clinical content, or token values. Counts, statuses, ids of
   non-person resources, and tenant ids are fine. Executors log
   `type(exc).__name__`, never `str(exc)` (may embed secret-bearing URLs).
2. **No secrets in code, tests, fixtures, or workflows.** No API keys,
   tokens, or webhook secrets — including "example" values that look real.
3. **Every FHIR resource access emits an AuditEvent** (reads and writes).
4. **Writes require step-up auth; clinical writes require human-in-the-loop**
   (HTTP 428 without `X-Human-Confirmed`). New write paths must call
   `validate_step_up_token` with its default `require_scope='write'`.
5. **`validate_step_up_token` returns `(bool, str)` — always destructure.**
   Coercing the tuple to a boolean is a silent auth bypass.
6. **Tenant isolation:** every `R6Resource` (and sibling-table) query filters
   by `tenant_id`; tenant comes from the `X-Tenant-Id` header, never the body.
7. **Redaction on external read paths** — responses leaving the guardrail
   boundary use `apply_redaction` / `apply_patient_controlled_redaction`.
8. **Clinical outputs carry disclaimers** and are decision support, never
   diagnosis. Honesty postures (calculator-not-eCQM, deny-list-not-authoritative)
   must not be weakened by wording changes.

## Correctness & testing

9. **Tests accompany behavior changes** — TDD preferred; at minimum, a
   regression test that fails without the change. Reference-only examples
   (`adapters/examples/`) are exempt from CI tests but must state so.
10. **Postgres/SQLite parity trap:** the test suite runs on SQLite, prod is
    Postgres. Schema-affecting changes (column widths, constraints) need a
    model-level assertion test (see `tests/test_ingest_resilience.py`), and
    new columns rely on `schema_sync` (additive + widen only). The
    `postgres-tests` CI job runs the DB-shape-sensitive subset
    (`tests/actions/`, ingest/fasten/models tests) against a real
    postgres:16 service container — schema-affecting tests belong in that
    subset, not just under SQLite.
11. **External payload shapes are pinned by tests** using real captured
    payloads (see `tests/test_fasten_webhook_shape.py`). Handlers for
    webhooks/callbacks must tolerate envelope nesting and fail without
    poisoning the DB session (rollback per failure).
12. **Live-path changes** (OAuth flows, webhooks, downloads) note how they
    were verified against the real external system.

## Drift guards (stale-number check)

13. If the MCP **tool count** changes, update ALL of: `services/agent-orchestrator/src/tools.ts`,
    `tools.test.ts` (names + counts), `adapters/tools.manifest.json`
    (`tool_count` + entry), `README.md` (badge, anchors, text, table),
    `templates/index.html` (counter + text), `templates/wiki.html`,
    `docs/recipes/any-agent-framework.md`, `docs/quickstarts/mcp-generic.md`.
14. Version strings live in `pyproject.toml` (canonical), `package.json`,
    `.health-context.yaml`, `templates/base.html`, README badges,
    `gemini-extension.json`, `server.json`. Don't update one without the rest.

## Style & scope

15. Match surrounding code: module pattern is pure engine + report builders +
    `register_*_routes(blueprint, deps)`; routes own auth/audit/store I/O.
16. `ruff` clean on touched Python; `tsc --noEmit` + jest green for the MCP
    server; compose changes validate with `docker compose config`.
17. Scope is honest: the PR does what its title says, states deliberate
    boundaries explicitly, and doesn't smuggle unrelated changes.
18. Python 3.11 compatibility (CI) — no 3.12+-only syntax (e.g. backslashes
    inside f-string expressions).

## Tone for reviews

Be specific and kind. Name what's good. Every REQUEST_CHANGES must say
exactly what to change and why, with a suggested fix where possible.
First-time contributors get a welcome.
