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
4. **Writes require step-up auth; clinical writes require human-in-the-loop.**
   New write paths must call `validate_step_up_token` with its default
   `require_scope='write'`. Two mechanisms exist — flag any confusion between
   them:
   - **Action rail:** approval is a *separate endpoint* consuming a single-use
     step-up credential. Never accept a header as approval here.
   - **Direct clinical FHIR writes:** currently HTTP 428 without the
     client-supplied `X-Human-Confirmed` header — spoofable by any agent
     holding a write token. Known gap (#214). **Reject new write paths that
     rely on this header**; route them through the action rail instead.
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

## Table stakes (docs/constitution.md + design.md)

The deterministic parts of these are enforced by `scripts/check_table_stakes.py`
in CI, on added lines only. The items below need judgment, so they are yours.

19. **One control, one property.** For every check, guard, assertion or
    conditional the PR adds: name the single property it protects, then ask
    what *else* could make it pass. If the broken behaviour also passes, the
    check is decoration — REQUEST_CHANGES with the specific input that would
    slip through. This project has shipped six of these; the pattern is a
    control that quietly does two things (a charset check carrying a length
    cap, `assert status in (400, 403)` accepting both fix and bug, a monitor
    counting how many checks ran rather than which).
20. **Load-bearing tests are mutation-tested.** A PR fixing a bug should show
    that reverting the fix turns a test red. Ask for it when a security or
    correctness property is claimed and the test would pass either way.
21. **Feature and hardening ship together.** If a PR adds a capability whose
    review raised security findings, both land in the same merge or neither
    does. Splitting them has put live vulnerabilities into production here.
22. **Deep modules.** A new interface should hide more than it exposes. Flag
    wide interfaces over thin wrappers, and any function whose correct use
    requires knowing its internals.
23. **Seams and fakes.** Anything crossing a boundary (HealthClaw client, LLM
    provider, FHIR server, mail, clock) goes through an adapter and is stated
    to have been verified against the real system. A fake proves a call is
    *made*, not *accepted*.
24. **Docs change with behaviour.** If the PR changes what the system does and
    a doc still describes the old behaviour, that is a defect in this PR — not
    a follow-up. Where a claim can drift, ask for a guard.
25. **Writing.** Applies to prose, PR text, comments, error messages and
    interface copy: one name per thing (a tenant is never also a workspace),
    active verbs, no marketing adjectives, no stacked hedges, and limits
    stated in the same breath as claims. Semicolons are fine in engineering
    prose; in patient-facing copy and error messages, prefer two sentences.
26. **Interface changes follow design.md.** Tokens, type and radius come from
    the file rather than being invented per component. The constraints that
    outrank taste: CSP `default-src 'self'` (no CDN assets), in-app webviews
    are a first-class target, 44px tap targets, 16px minimum on inputs,
    `prefers-reduced-motion` honoured, WCAG AA. If a change needs a new token,
    it changes `design.md` in the same PR.

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
