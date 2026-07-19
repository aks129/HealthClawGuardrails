# Picking up an issue — guide for coding agents and collaborators

If you (human or coding agent) are starting work on an issue in this repo, read
this first. It exists so you don't begin from scratch: it tells you where things
live, what you may not break, and what "done" means here.

**Read order:** this file → [docs/development.md](development.md)
(build/test/deploy detail) → the issue.

> Maintainers may also have a local `CLAUDE.md` with the same invariants. It is
> deliberately not published, so everything you need is here or in
> `docs/development.md` — if something is only in `CLAUDE.md`, that's a bug in
> this guide and worth an issue.

---

## 1. What this project is, in one paragraph

HealthClaw Guardrails is a safety layer between AI agents and FHIR health data.
Guardrails run **server-side** — redaction on reads, step-up + human confirmation
on writes, audit on everything — so a client cannot bypass them. That single
sentence explains most design decisions you'll encounter. If a change would let
a *client* decide whether a guardrail applies, the change is wrong regardless of
how clean the code is.

---

## 2. The invariants — non-negotiable

These are enforced by the conformance harness
(`tests/test_guardrail_conformance.py`, a CI gate that must stay **Grade A**).
Breaking one fails CI, but more importantly it breaks the product's core claim.

- `validate_step_up_token` returns `(bool, str)` — **destructure both**. Never
  truthiness-test the tuple; a non-empty tuple is always truthy, so this silently
  authorizes everything.
- **Every FHIR resource access emits an AuditEvent**, and audit `detail` stays
  **PHI-free**. Never interpolate a caller-supplied value into audit detail.
- Writes require a step-up token. **Clinical** writes additionally require
  out-of-band human confirmation via a separate approval endpoint. There is no
  header that grants this — the old spoofable `X-Human-Confirmed` is gone, and
  nothing may reintroduce that pattern.
- No code path may let an agent approve its own action.
- Redaction goes through `r6.redaction`: `apply_redaction` (Safe Harbor) or
  `apply_patient_controlled_redaction(resource, patient_id)`.
- **"No known allergies" is never inferred** — only from an explicit human
  attestation in the SDC populate/review flow.
- Resource identity is composite: `(tenant_id, resource_type, id)`. **Every
  query must be tenant-scoped.** Ids can collide across tenants.
- CareAgents and SmartHealthConnect store **no PHI**.
- Never print, log, or commit secrets or PHI. Demo/test data is synthetic only.

### The reflection rule (easy to get wrong)

Do not copy caller-supplied or backend-supplied text into error messages,
warnings, or audit detail. The established pattern is an **allowlist that
constructs rather than filters**: derive the output from a finite, code-owned
set, and fall back to a generic message when input isn't in it. See
`_safe_unsupported_key` in `r6/routes.py` and `sanitizeOperationOutcome` in
`services/agent-orchestrator/src/backend-failure.ts` for the two reference
implementations — one Python, one TypeScript.

---

## 3. Where things live

```text
r6/                      Flask FHIR facade + guardrail engine
  routes.py              REST facade (large — see #56 for the carve-up plan)
  redaction.py           Safe Harbor + patient-controlled redaction
  audit.py, models.py    AuditEvent + persistence
  actions/               action rail — the plugin surface for real-world actions
    rails/*.py           ActionExecutors, registered in rails/__init__.py
  quality/ labs/ sdc/ conformance/ shc/ smbp/ wearables/
                         pure engines + register_*_routes(blueprint, deps)
careagents/              hosted consumer app — SEPARATE Flask app, no PHI
services/agent-orchestrator/   Node/TypeScript MCP server
tests/                   pytest; fixtures in conftest.py
e2e/                     Playwright
```

**The module pattern.** New feature modules follow `r6/quality`: a **pure
engine** (no Flask, no DB), report builders, and a `register_*_routes(...)`
wired into `r6/routes.py`. Follow it — it's what makes the engines testable
without a request context.

**The action rail is the extension point.** Adding a real-world capability means
writing an `ActionExecutor` and registering it — it then inherits the whole rail
(propose-time validation → out-of-band human gate → audit → reconciliation)
without touching core code. See
[docs/extending-the-action-rail.md](extending-the-action-rail.md). If you find
yourself building a *new* approval mechanism, stop: use the rail.

**The engine/surface split.** This repo is the engine; CareAgents
(`careagents/`, in-repo) is the consumer surface. The split is declared in
`.health-context.yaml` (`role: engine`, with surfaces listed). Surfaces never
read FHIR directly. SmartHealthConnect, the original external surface, was
archived 2026-07-19 after violating exactly that rule — its skills live on as
CareAgents advisors (`careagents/advisors.py`).

---

## 4. Build, test, verify

`.env` is **not auto-loaded** — export vars in your shell. A key present only in
`.env` behaves as unset.

```bash
uv sync
STEP_UP_SECRET=dev-secret python main.py            # http://localhost:5000

uv run python -m pytest tests/ -q                    # all Python tests
uv run python -m pytest tests/test_r6_routes.py::test_name -v   # one test
uvx ruff check .                                     # lint (CI-gated)

cd services/agent-orchestrator && npm ci && npx tsc --noEmit && npm test
```

**CI runs Python 3.11**; local dev works on 3.13. Avoid 3.12+-only syntax (e.g.
backslash escapes inside f-string expressions) or CI will fail on code that
passes locally.

**There is a Postgres CI lane** because SQLite masks a real bug class (varchar
length limits). If you add a column, match its width to real values — and know
that a local SQLite-only run does **not** prove the Postgres lane passes.

Revert incidental `uv.lock` churn before committing.

---

## 5. Definition of done

An issue is done when all of these hold. State them explicitly in the PR.

- [ ] `uv run python -m pytest tests/ -q` passes — quote the actual counts
- [ ] `uvx ruff check .` clean
- [ ] Node changes: `npx tsc --noEmit` clean and `npm test` passes
- [ ] Conformance still **Grade A** (`tests/test_guardrail_conformance.py`)
- [ ] New behavior has tests — including a **negative** test (the guardrail
      actually refuses the thing it claims to refuse)
- [ ] No caller/backend text reflected into errors, warnings, or audit detail
- [ ] Every new query is tenant-scoped
- [ ] Commits signed off (`git commit -s`, DCO — no CLA)

**Report results honestly.** If tests fail, say so and quote the output. A PR
description that claims a passing suite it didn't run is worse than a failing
PR — this is health infrastructure, and the review process assumes descriptions
are true. Scope security claims to what you actually changed: saying "the layer
never forwards backend text" when you fixed six call sites will mislead the next
auditor.

---

## 6. Traps that have actually bitten this project

- **Version/tool-count drift.** `tests/test_site_version_sync.py` and
  `tests/test_gemini_extension.py` fail if `pyproject.toml`, the manifests, the
  README, and the site templates disagree. Bump them together; see
  [RELEASING.md](../RELEASING.md).
- **The MCP server does not auto-deploy.** Pushing `main` deploys the Flask app
  (Railway) and site (Vercel), but MCP tool changes are live in the repo and
  *not* in Claude until a manual deploy runs.
- **CareAgents deploys independently** to a shared prod VPS. That deploy needs
  explicit authorization — don't roll it into unrelated work.
- **Playwright e2e is currently red on `main`** for environment reasons, so it
  gives no signal. Don't read a green/red Playwright result as evidence either
  way until that's fixed.
- **`create_all` never alters existing tables.** Adding a column to a model needs
  a migration; CareAgents uses an idempotent `_ensure_columns(engine)` for this.
- **A stale branch can silently drop work.** If your branch predates a squash
  merge of the same feature, merging can revert files. Rebase onto current
  `origin/main` and confirm the files you expect are present before pushing.

---

## 7. Working style

- Ask before large refactors of `r6/routes.py` — a carve-up is already planned
  (#56) and uncoordinated splits will conflict.
- Prefer small, reviewable PRs. Every PR gets an automated standards review
  ([.github/REVIEW_STANDARDS.md](../.github/REVIEW_STANDARDS.md)) plus CI.
- **A maintainer approves before merge.** Agent-authored PRs do not
  self-merge — the human stays the final authority on this project.
- If an issue's requirements turn out to be wrong once you're in the code, say
  so in the issue rather than silently building something different.

---

## Related

- [docs/development.md](development.md) — full contributor guide
- [docs/healthcare-ai-advisors-roadmap.md](healthcare-ai-advisors-roadmap.md) — where this is all going
- [CONTRIBUTING.md](../CONTRIBUTING.md) — ground rules, DCO
- [GEMINI.md](../GEMINI.md) — how agents should behave when *calling* the deployed tools
