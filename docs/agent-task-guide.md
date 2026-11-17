# Picking up an issue — guide for coding agents and collaborators

If you (human or coding agent) are starting work on an issue in this repo, read
this first. It exists so you don't begin from scratch: it tells you where things
live, what you may not break, and what "done" means here.

**Read order:** this file → [docs/constitution.md](constitution.md) (how we
build — architecture, writing, working with agents) → [docs/development.md](development.md)
(build/test/deploy detail) → the issue.

If the task touches any interface, read [design.md](../design.md) too. It
documents the tokens and type that actually ship, and the constraints — CSP,
in-app webviews, reduced motion — that outrank visual preference.

> Maintainers may also have a local `CLAUDE.md` with the same invariants. It is
> deliberately not published, so everything you need is here or in
> `docs/development.md` — if something is only in `CLAUDE.md`, that's a bug in
> this guide and worth an issue.

---

Before parallel work, follow the [worker coordination protocol](runbooks/worker-coordination.md)
and read the shared ownership/handoff log linked there.

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
  out-of-band human confirmation. The mechanism is the action rail's separate
  approval endpoint, which consumes a single-use credential bound to the
  action. No header grants this. `X-Human-Confirmed` is gone from the action
  rail, but direct clinical FHIR writes still accept it (HTTP 428 without
  it). The caller sets it about itself, so it is a known gap (#214), not a
  human gate. Do not build new write paths on it, and do not reintroduce the
  pattern anywhere.
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
uv run ruff check .                                  # lint (CI-gated) — not uvx/pipx

cd services/agent-orchestrator && npm ci && npx tsc --noEmit && npm test
```

**CI runs Python 3.11**; local dev works on 3.13. Avoid 3.12+-only syntax (e.g.
backslash escapes inside f-string expressions) or CI will fail on code that
passes locally.

**There is a Postgres CI lane** because SQLite masks a real bug class (varchar
length limits). If you add a column, match its width to real values — and know
that a local SQLite-only run does **not** prove the Postgres lane passes. The
lane runs the whole suite (#232), so there is no list to add your test to. To
reproduce it locally, start a throwaway `postgres:16` container on a free port
(not 5432) and export `SQLALCHEMY_DATABASE_URI`, `MIGRATION_TEST_DATABASE_URL`
and `CARE_TEST_DATABASE_URL` pointing at it before running pytest. A test that
builds its own engine or `AccountService` must dispose it in teardown, or the
suite runs out of Postgres connections partway through.

Revert incidental `uv.lock` churn before committing.

---

## 5. Definition of done

An issue is done when all of these hold. State them explicitly in the PR.

- [ ] `uv run python -m pytest tests/ -q` passes — quote the actual counts
- [ ] `uv run ruff check .` clean
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
- **CareAgents deploys independently** — a manual staged `railway up`, web and
  worker. That deploy needs explicit authorization — don't roll it into
  unrelated work. The VPS script it used before is retired and refuses to run.
- **Playwright e2e is currently red on `main`** for environment reasons, so it
  gives no signal. Don't read a green/red Playwright result as evidence either
  way until that's fixed.
- **`create_all` never alters existing tables.** Adding a column to a model needs
  a migration; CareAgents uses an idempotent `_ensure_columns(engine)` for this.
- **A stale branch can silently drop work.** If your branch predates a squash
  merge of the same feature, merging can revert files. Rebase onto current
  `origin/main` and confirm the files you expect are present before pushing.
- **A request body is parsed before your handler runs.** `enforce_human_in_loop`
  (`r6/health_compliance.py`) is a `before_request` hook on `r6_blueprint`, and
  it parses every non-exempt POST/PUT body ahead of every handler. A guard you
  add *inside* a handler is therefore unreachable for anything the parse itself
  raises. This produced two unauthenticated 500 levers — a deep-nesting
  `RecursionError` and a non-object body `AttributeError` — and both times the
  handler-level fix looked correct, passed review, and changed nothing. If you
  are hardening a write path against a malformed body, check what runs *above*
  the handler first.
- **A strict-xfail row in `tests/test_write_guard_matrix.py` goes red when you
  FIX the defect it pins.** That is deliberate: a fix must not land while the
  table still describes the route as broken. The consequence is that a fix PR
  updates its own matrix row in the *same* PR. Three security PRs skipped that
  and left `main` red for a day, each of them individually green. The matrix
  must report **0 failed and 0 xpassed** — an XPASS is a failure, not a pass.
- **A maintainer's approval is the merge, not a verdict on it.** The
  `auto-merge-when-satisfied` job in `.github/workflows/claude-pr-review.yml`
  runs `gh pr merge --squash --auto` as soon as it sees an approving review
  from an OWNER, MEMBER or COLLABORATOR. Once armed, GitHub merges when the
  *required* checks pass — `main` requires eight, and every other base branch
  requires none, so on a stacked PR arming is merging. The job fires on
  `opened`/`synchronize`/`reopened`/`ready_for_review` and not on the review
  event, which makes this worse rather than milder: a standing approval arms
  on somebody else's later push, hours on, with the approver no longer
  watching. There is no "approve now, merge when I say so" state — approve
  only what you are willing to have land unattended. For the same reason,
  check `gh pr view <n> --json autoMergeRequest` before you approve *and*
  before you fix whatever is holding a PR red: an armed merge fires the moment
  the last check goes green, including the check you just fixed, and
  `--disable-auto` cannot help once it has fired. On 2026-08-16 it fired on
  exactly that: a lint fix turned the last check green, #519 merged while the
  problem in it was still being worked on, and what it put on public `main`
  had to be corrected afterwards (#527).
- **Never read a stacked pull request's green as tested.** `ci.yml` filtered
  `pull_request` on `branches: [main, master]`, and that filter matches the
  pull request's *base* — so a change stacked on another feature branch matched
  nothing, and the whole workflow, every test lane in it, was skipped.
  Measured 2026-09-03: **19 checks** on a pull request into `main`, **6** on a
  stacked one, and none of the six was a test. One of the six was
  `auto-merge-when-satisfied`, which is how the trap above becomes a merge with
  zero tests behind it. #585 has the per-job detail; #588 drops the filter.
  Two things survive that fix. Feature-branch bases carry no protection at all
  (`GET /branches/<branch>/protection` returns 404), so with the filter gone a
  red test on a stacked pull request is *visible* but still blocks nothing. And
  CodeQL here is GitHub default setup, which runs only against the default
  branch, so even once #588 has landed a stacked pull request gets 15 checks
  where one into `main` gets 19. Count the checks on a stacked pull request; don't read the tick.
- **A `pull_request_target` fix cannot show on its own pull request.** That
  event runs the base branch's copy of the workflow, never the PR's. #643
  moved the second reviewer's trigger to `pull_request_target` and left its
  job condition on `pull_request`. The job skipped itself on every pull
  request, and a skipped job reads as green in a check list. #651 fixed the
  token, and its own run was still decided by main's broken file. Verify such
  a fix on the first pull-request event after it merges, not before. The
  property is pinned: `tests/test_ci_hardening.py` refuses a job condition
  that names an event its workflow never listens for.
- **A "pinned set" of tests is a guess about what you touched.** Twice on
  2026-09-06 a change went green on the files named in the PR and red on the
  full suite: `tests/actions/` is a subdirectory the pinned set never
  listed, and a line-keyed allowlist in `tests/test_access_kernel.py` moved
  when lines were added above the call it named (#660). Run the whole suite
  before opening a pull request, in the background if it must be, and read
  the exit code rather than the last line.
- **Postgres enforces what SQLite ignores.** `alembic_version.version_num` is
  `VARCHAR(32)`. A 39-character revision id upgraded cleanly on every SQLite
  run and failed the Postgres lane on the first deploy-shaped test with
  `value too long for type character varying(32)` (#658).
  `tests/test_database_migrations.py` now refuses a revision id over 32
  characters; read the Postgres lane on its own, not the aggregate green.
- **A comment can cancel a review.** A workflow with several triggers and
  `cancel-in-progress: true` puts every kind of run in one concurrency group
  unless the group names the event. The Vercel bot's deployment comment,
  two seconds after a pull request opened, fired the reviewer's
  `issue_comment` run and cancelled the review it was supposed to leave
  alone (#653, fixed in #654 with a test).
- **A hash stored beside the thing it attests attests nothing.** The first
  human-gate digest (#658) was a sha256 in the same table a writer with
  database access could rewrite; forge the payload and the digest together
  and the audit line vouched for the forgery. An adversarial pass against a
  live database found it in an hour. Key a digest with a secret the
  database does not hold (the step-up secret), and ask for that kind of
  pass on any guardrail change before it is armed.
- **An SDC definition names its resource type twice.** The StructureDefinition
  URL before `#` and the element path after it both carry a type, and a
  check that reads one lets `AllergyIntolerance#Patient.name.given` file an
  allergen into the Patient (#664). Check both where the URL names a type;
  say plainly that a profile URL cannot be checked without resolving it.

---
- **A mutation that does not compile reads as caught.** A TypeScript mutation
  that broke the types made ts-jest fail before any test ran, and the runner
  reported `Tests: 0 total` with a non-zero exit, which a script that checks
  only the exit code prints as CAUGHT (2026-09-06, the MCP server's OAuth
  path). Read the summary line: a mutation is caught when tests ran and some
  failed, not when nothing ran. Write the mutation so it compiles (an
  environment-variable guard instead of a literal `false` keeps the type
  narrowing) and count the failures.
- **SQLite also ignores foreign keys, and no test deletes the parent.** A
  grant table pointed at the connections table; `delete_connection` removed
  the connection row; every SQLite run passed and Railway Postgres would have
  refused the first real delete (#670, found in review). The Postgres lane
  only catches what a test exercises, so when a new row references an old
  one, write the test that deletes the old one, on both lanes.
- **An assertion joined with `or` may not be able to fail.** `assert a == b or
  c.startswith(...)` passed for a reason that had nothing to do with the
  property; the second clause was always true. Read every `or` in an assert
  as "which half could ever be false here"; if the answer is neither, the
  test measures nothing (found in review, 2026-09-06).
- **`HealthClawClient` caches its `/r6/fhir` base at construction.** A test
  that repoints `client.base` to a dead port to simulate an outage leaves
  `client.fhir` pointing at the live one, and the outage never happens;
  set both (live-probe session, 2026-09-06, probing #670's revoke path).

## 7. Working style

- Ask before large refactors of `r6/routes.py` — a carve-up is already planned
  (#56) and uncoordinated splits will conflict.
- Prefer small, reviewable PRs. Every PR gets an automated standards review
  ([.github/REVIEW_STANDARDS.md](../.github/REVIEW_STANDARDS.md)); how much CI
  it gets depends on its base branch, which §6 covers.
- **A maintainer approves before merge.** Agent-authored PRs do not
  self-merge — the human stays the final authority on this project. That
  approval is also the merge instruction (§6), so it is the last point at
  which anything is decided, not a checkpoint before one.
- If an issue's requirements turn out to be wrong once you're in the code, say
  so in the issue rather than silently building something different.

---

## 8. Working alongside the other agent

Two agents work this repository, in different tools, on the same clock. They
cannot see each other's sessions, and neither can read the other's local
notes. What they share is git and GitHub, so every rule below is expressed in
something both can read from a terminal.

### Before you start: ask whether anybody is already in there

```
scripts/lane_check.py r6/access.py careagents/
```

It fetches, then reports every open pull request, every checked-out worktree
and every claimed issue that already touches those paths, and exits non-zero
when it finds one. Exit 2 means it could not answer — no `gh`, a failed fetch
— which is not a green light.

Run it before the first edit, not after. The expensive collision is not two
commits on one line; it is two half-finished branches doing the same job,
found at review time.

### Lanes

Ownership is by path, because that is what a diff collides on. The `area:`
labels on issues already name the same split, so an issue's label tells you
whose lane its work lands in.

| Paths | Lane |
| --- | --- |
| `r6/access.py`, `r6/read_auth.py`, `r6/stepup.py`, `r6/oauth.py`, `r6/audit.py`, `r6/redaction.py` | guardrail core |
| `services/agent-orchestrator/`, the OAuth spec and its runbook | connector chain |
| `careagents/` | consumer app |
| `docs/quickstarts/`, `docs/evidence/`, partner and ecosystem material | adoption |
| `r6/sdc/`, `r6/actions/`, clinical rails | clinical |

A lane is not a fence around a person. It says who to expect in a file, so
that finding somebody else there is a signal rather than a surprise.

**Shared surfaces** — `docs/`, `tests/`, CI workflows, the guide you are
reading — belong to nobody and are touched by both. The rule there is
narrower: name the shared file in the pull request body, so the other agent
finds it by reading titles rather than by hitting a conflict.

### Claiming

The claim is the GitHub issue, because it is the only lock both tools can see
and it survives a session ending.

- Assign the issue to yourself before branching. No branch off `main` for
  work you have not claimed.
- A claim with no pushed branch after a few hours is stale; unassign it or say
  on the issue why it is still yours.
- Found something while working on something else? File the issue rather than
  widening the branch. Two of today's findings were filed that way and neither
  disturbed the pull request that produced them.

### Branches and worktrees

- **Prefix the branch with your lane**, so `git branch -r` reads as a
  division of labour. `codex/` is taken.
- **One worktree per branch, and the path says which**: `wt-<issue>` under
  your own scratch directory. Never adopt a worktree you did not create, even
  when the branch you want is already checked out in it — that is the
  collision the rule exists to prevent.
- **Push every branch you intend to keep.** An unpushed branch is invisible to
  the other agent and to `lane_check.py`, and it is the one kind of work that
  can be silently duplicated.
- Remove your worktree when its pull request merges. Nobody else can tell
  whether yours is finished.

### Where the shared state lives

Anything the other agent needs and cannot derive from the code — which
branches are pushed but unopened, what they were cut from, the order they
have to land in — goes on the board issue, **Held branches, fork points and
the order they have to land in** (#685), not in a local note. Edit the issue
when the state changes. A private file that only one agent reads is how two
agents end up rebasing the same branch twice.

### Staying current

Start every working session, and every long turn, with the same two commands:

```
git fetch origin && git log --oneline -1 origin/main
gh pr list --state open --json number,title,headRefName
```

Read the other agent's pull request titles, not only your own. That is what
tells you a path has been taken since you last looked.

### Reviewing each other

Findings on the other agent's pull request go in a comment, with the
reproduction that produced them. Do not approve it and do not merge it: the
maintainer's approval is the gate (§7), and an agent approving an agent
removes the only human step in the chain.

---

## Related

- [docs/constitution.md](constitution.md) — how we build: deep modules, seams,
  one-control-one-property, the writing rules, working with agents
- [design.md](../design.md) — the design system both surfaces actually ship
- [docs/development.md](development.md) — full contributor guide
- [docs/healthcare-ai-advisors-roadmap.md](healthcare-ai-advisors-roadmap.md) — where this is all going
- [CONTRIBUTING.md](../CONTRIBUTING.md) — ground rules, DCO
- [GEMINI.md](../GEMINI.md) — how agents should behave when *calling* the deployed tools
