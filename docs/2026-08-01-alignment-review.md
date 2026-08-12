# Codebase ↔ Vision Alignment Review — 2026-08-01

Seven parallel audits (guardrails, architecture, user layer, integrations, actions,
market, dev system) against one anchor:

> A patient spins up a persistent, guardrailed health agent in a minute; it connects
> their real records, answers safely, and **accomplishes real-world actions on their
> behalf** with human approval — delivered to actual real users, with HealthClaw as
> the standard-setter for agent guardrails in healthcare.

Every finding below was verified by reading code, not comments.

---

## 0. Stop-the-line finding

**`POST /r6/fhir/demo/agent-loop` is an unauthenticated cross-tenant write and
policy-delete primitive, live in production right now.**

- `/demo/` is prefix-exempt from tenant enforcement (`r6/routes.py:180-186`) and from
  human-in-the-loop (`r6/health_compliance.py:109`).
- The handler validates **no step-up token**. The two `generate_step_up_token()` calls
  (`r6/routes.py:2249`, `:2432`) discard their return values — they are theater.
- It reads `X-Tenant-Id` from the header (default `demo-tenant`), then commits a
  Patient, **soft-deletes every existing Permission resource for that tenant**
  (`r6/routes.py:2318-2323`), and commits a Permission and an Observation.
- Verified live: `POST https://app.healthclaw.io/r6/fhir/demo/agent-loop` with no
  auth returns **200**.

Any anonymous caller can write into — and disable the access-control policy of — any
tenant they can name. This directly falsifies the project's flagship claim that a
client cannot bypass the guardrails. **Fix before anything else on this page.**

Fix: restrict to `is_public(tenant_id)` tenants **and** require `X-Internal-Secret`,
or disable in production outright. One day of work.

---

## 1. Alignment scorecard

| Dimension | Grade | Where code and story diverge |
|---|---|---|
| Guardrail framework | **C+** | Core primitives (audit fail-loud, step-up design, action state machine) are genuinely excellent. But the demo endpoint above, a self-confirmable human gate, and read-auth-off-by-default mean the *system* is weaker than its *parts*. Grade A is earnable with all four live. |
| Architecture | **B−** | PHI boundary is clean and real. Fails first at ~1 heavy user (unbounded history) and ~10-30 concurrent (thread saturation → healthz timeout → restart → amnesia). |
| User layer | **C** | Honesty is engineered in, delete flow is best-in-class. But the "persistent agent" is a goldfish, passkey enrollment is a dead loop, and the payoff moment underdelivers. |
| Integrations | **C+** | Fasten covers ~50-60% of clinical data self-serve. Wearables, insurer, pharmacy, file import: **0% self-serve.** Documents ingest but no tool reads them. |
| Actions | **D+** | Exactly **one** real-world action ships (intake-form PDF). This is the vision's core and its weakest column. |
| Market position | **B** | The unoccupied ground is real and verified. The moat is narrow and closing. |
| Dev system | **B−** | Strong CI. Zero production verification; the review bot enforces a stale invariant. |

**The pattern across all seven:** the *engine* is stronger than the *product*. We built
excellent guardrail primitives, then shipped a consumer app that can't yet use most of
them — while the market's read-only incumbents got 300M users.

---

## 2. Where the story is ahead of the code

These are places our own docs, copy, or claims currently overstate what ships. Each is
a trust liability for a health product.

| Claim | Reality | Where |
|---|---|---|
| "Client cannot bypass the guardrails" | Demo endpoint bypasses everything, unauthenticated, in prod | `r6/routes.py:2218` |
| "The spoofable `X-Human-Confirmed` header is gone" | True on the action rail only. It is still the **entire** human gate for direct clinical FHIR writes — and the conformance probe **spoofs the header itself**, so Grade A certifies a gate the grader just bypassed | `r6/health_compliance.py:91-131`, `r6/conformance/probes.py:230-233` |
| "No code path lets an agent approve its own action" | `/confirm` accepts the same token class as `/commit`, with no audience/operation binding. An agent holding a commit token can confirm its own action | `r6/actions/routes.py:460-470` |
| "Persistent health agent" | History is process-local, never rendered on reload, wiped by every deploy | `careagents/app.py:49` |
| "(sample data for now)" greeting | Shown to **every** agent, including real-record ones | `careagents/templates/chat.html:23` |
| "Email support to delete your data" (consent card) | Self-serve Delete shipped in #203 and sits on the same page | `careagents/templates/home.html:135` |
| "Add a passkey" link | `/auth` redirects logged-in users to `/home` — enrollment unreachable after signup | `careagents/app.py:82-84` |
| SHL paste / file upload tiles | Return `{"soon": True}` | `careagents/connectors.py` |
| `REVIEW_STANDARDS.md` item 4 | Describes the removed header — the AI reviewer's constitution is stale | `.github/REVIEW_STANDARDS.md` |

---

## 3. Unified gap register

Ranked by one question: **does this block a real user from getting real value — or
expose them?**

### P0 — before any real user (days)

| # | Gap | Source |
|---|---|---|
| 1 | `/demo/agent-loop` unauthenticated cross-tenant write/policy-delete **(live in prod)** | Guardrails |
| 2 | Bind the confirm credential (`require_audience='action-approval'` + operation). Curatr already does this correctly — copy the pattern | Guardrails, Actions |
| 3 | Unbounded chat history → runaway spend, then permanent context-overflow 400s for that tenant; tool loop has no hard stop | Architecture |
| 4 | `pool_pre_ping` / `pool_recycle` missing on managed Postgres — intermittent 500s post-migration | Architecture |
| 5 | Two silent lies: `mail.send_code` failure returns `{"sent": true}`; failed `confirm_action` swallowed after user approves | Architecture |
| 6 | Track + CI-build `deploy/careagents/Dockerfile` (still untracked) | Architecture |
| 7 | Fix stale `X-Human-Confirmed` in `REVIEW_STANDARDS.md` / `development.md` / `SECURITY.md` | Dev system |
| 8 | Nonce store: require `REDIS_URL` in production multi-worker, fail loud | Guardrails, Actions |

### P1 — makes the product worth returning to (1-3 weeks)

| # | Gap | Source |
|---|---|---|
| 9 | **Persist chat history** — decide storage side first (HealthClaw per-tenant keeps the no-PHI promise intact) | Architecture, UX |
| 10 | **Server-derived terminology labels** — kills the "unlabeled record, code X" cliff. Codes are PHI-free; this needs no redaction change | UX, Integrations, #207 |
| 11 | **`get_documents` tool** — DocumentReferences already ingest and count; nothing can read them | Integrations |
| 12 | **Tier-2 approve surface** — required for *every* future action, purely in our control, ~1 day | Actions |
| 13 | Enforce `CONTACT_NOT_ALLOWLISTED` + `DAILY_CAP_REACHED` (codes reserved, referenced nowhere) | Actions |
| 14 | Scheduled prod synthetic monitor — `scripts/careagents_smoke.py` already exists, just not on a cron | Dev system |
| 15 | Real greeting from actual records; kill "(sample data for now)" for real connections | UX |
| 16 | Passkey dead loop; replace `prompt()`/`alert()` flows; style conn-card buttons | UX |
| 17 | Read-auth: default-off and ungraded — a wide-open deployment still scores Grade A | Guardrails |
| 18 | Redaction misses on `SubscriptionTopic/$list`; `$share-bundle` intake exports identified records on a generic token | Guardrails |

### P2 — the differentiated bet (3-8 weeks)

| # | Gap | Source |
|---|---|---|
| 19 | **Pharmacy transfer/refill call end-to-end** — the flagship action. Unblock Bland (external, start now) | Actions, Market |
| 20 | Grade what we claim: probes for read-auth, spoofed-confirmation, step-up negatives, action-rail separation, redaction on search | Guardrails |
| 21 | Versioned guardrail spec + written threat model — a third party has nothing normative to conform *to* | Guardrails, Market |
| 22 | One proactive loop (weekly care-note via Resend) — the only thing on any list that answers "why come back" | UX |
| 23 | Real file-upload / SHL import — zero-integration path every patient can use | Integrations |
| 24 | Productize the MEDENT SMART flow (DB-backed broker) — turns a proven connection into something patients can click | Integrations |
| 25 | Retire `X-Human-Confirmed` for direct clinical writes; route through the propose→approve rail | Guardrails |
| 26 | CareAgents Playwright spec (none exists — all e2e targets the HealthClaw site) | UX, Dev system |
| 27 | Purge orphans: `ActionEvent` / `ActionConfirmation` (no `tenant_id` at all) | QA, Actions |

---

## 4. Sequenced roadmap

**Week 1 — Close the exposure.** P0 items 1-8. Nothing here is user-visible; all of it
is the difference between "we have guardrails" being true and being marketing.

**Weeks 2-3 — Make the payoff land.** Items 9-12, 14-16. The through-line: a user who
connects real records today gets amnesia, unreadable labels, and unreadable documents.
Three fixes turn the same connection into a real product. Ship the approve surface in
this window regardless of Bland — it's the gate for every future action.

**Weeks 4-8 — Ship the differentiator.** Item 19 as the headline, 20-21 as the
standard-setter proof, 22 as retention. Start Bland onboarding *now*, in week 1, since
it's the external critical path.

**Sequencing logic:** P0 protects users who don't exist yet — but the HIMSS webinar
(Aug 18) and any design-partner demo make the demo endpoint a live exposure today. P1
is what makes a first user return. P2 is the only thing that distinguishes us from
ChatGPT Health, which shipped nationwide nine days ago.

---

## 5. Strategic frame

Verified unoccupied ground:

1. **The consumer action rail.** ChatGPT Health, Claude+HealthEx, Perplexity Health —
   all read-only. Amazon acts, but only inside its own clinic. Provider-side voice AI
   (Hippocratic, Talkie) acts for the practice. A patient-owned agent that executes at
   *any* provider behind propose→human-approve→audit is genuinely unclaimed.
2. **Verifiable safety substrate.** Innovaccer's HMCP is the only comparable artifact —
   a spec plus a commercial gateway for health systems. Open-source engine + live
   `$conformance` grading is a different, checkable claim. *Provided the grade is honest*
   — see §2, which is why item 20 is strategic, not hygiene.
3. **The regulated on-ramp.** CARIN-CFA → Medicare App Library (CMS-recognized Feb 2026)
   is a distribution channel Big AI can't easily use.

Not a moat: "agent in a minute" (incumbents do record-grounded chat for 300M weekly
health users) and raw connectivity (b.well's 1.7M providers dwarfs ours).

Threats, in order: OpenAI/Anthropic adding actions; Amazon widening past its clinic;
HMCP becoming the standard while we remain a project. The third makes the Bo Holland
agent-identity thread and HL7/CARIN engagement **existential, not optional**.

**First-1000 playbook** (from Solace, Guava, PicnicHealth, Ada): pick one population
(Medicare + small independent practices), make it free where a practice or sponsor
pays, and publish the trust artifact — **"X actions completed, 100% human-approved, 0
safety incidents."** That counter is the consumer analogue of the "115M interactions"
stat that raised Hippocratic $126M. It also converges exactly with item 19: the approve
surface plus one real action is what makes the number exist.

---

## 6. Standing sub-agent roster

Each is grounded in a defect this repo actually shipped.

| Agent | Trigger | Non-negotiable checklist (abridged) |
|---|---|---|
| **Guardrail Sentinel** | Every PR touching redaction / audit / step-up / actions / MCP error paths | No PHI or token in any log, audit `detail`, or error body (`str(exc)` vs `type(exc).__name__`); `validate_step_up_token` destructured; every access audited; tenant from header never body; no path where an agent approves its own action |
| **Conformance Warden** | Conformance code, the four invariant docs, weekly sweep | Grade A never weakened to pass; `REVIEW_STANDARDS.md` ≡ `CLAUDE.md` ≡ `development.md` ≡ code; honesty postures not softened by wording edits; **first task: the stale `X-Human-Confirmed` text** |
| **Onboarding Shadow** | Weekly cron + post-deploy | Full `careagents_smoke.py` incl. one real LLM turn; signup under a minute; synthetic tenants only; P1 issue with repro on failure. *Would have caught the broken-login bug the day it shipped* |
| **Patient Advocate** | Every PR touching user-facing copy or templates | Nothing advertised that isn't wired (#170); consent at the real-records moment (#172); delete reachable and honest (#203); errors tell a scared human what to do and leak nothing |
| **Release Captain** | Release cut; monthly if main >4 weeks past tag | All 8 drift-guard locations bumped together; `demo_e2e.sh` gates; post-deploy metadata + `tools/list` + Grade A; **never deploys MCP or CareAgents prod without explicit go** |
| **Dependency Quartermaster** | Weekly + on audit-gate failure | Grouped minor/patch auto-merge; majors summarized with risk; unfixed advisory → scoped override same day (#197 pattern); no incidental `uv.lock` churn |
| **Market Scout** | Weekly light / monthly deep | Every finding becomes an issue or is dropped; regulatory findings flagged before any public claim changes; **drafts only, human-gated** |

Wiring: Sentinel, Warden, and Advocate are path-triggered additions to
`claude-pr-review.yml`. Captain, Shadow, Quartermaster, Scout are scheduled/on-demand.

---

## 7. Research calendar

| Cadence | Loop | Why here specifically |
|---|---|---|
| Weekly | Dependabot triage + prod smoke | Audit gate has broken main twice; login broke silently once |
| Weekly | MCP spec + Anthropic platform changes | The MCP server *is* a product surface; transport auth fail-closed prod once |
| Biweekly | FHIR R6 ballot progress | CI asserts the `6.0.0-ballot3` string — a bump is a breaking event |
| Monthly | FTC HBNR + OCR enforcement vs consumer health apps | CareAgents is a non-covered-entity consumer app; **decision on #168 still open** |
| Monthly | CARIN Alliance + agent-identity standards | Standard-setter thesis; Bo Holland thread live |
| Monthly | Connector drift: Open Wearables, Fasten, MEDENT/Epic | OW 0.6.3 already broke docs-vs-reality (#127) |
| Monthly | Competitor scan | Feeds HIMSS Aug 18 and the ecosystem campaign |
| Quarterly | Security posture; re-pin the security-baseline commit | Pinned to a July commit by design — someone must consciously re-pin |

---

## 8. Three process changes with the best velocity-per-risk

1. **Scheduled prod synthetic monitor** (~1 hour). Every 6h: smoke script, `$conformance`
   Grade A assertion, `/healthz`, MCP `tools/list`. Failure opens a pinned issue — the
   alerting channel a solo maintainer will actually see.
2. **Move the strict dependency audit off the PR critical path** — daily scheduled job
   that opens an issue; PRs gate only on lockfile changes. Add the expiring-exception
   policy file `ci.yml` already promises but doesn't have.
3. **Make the Postgres lane self-selecting** (marker + auto-marking conftest rule) and
   add a nightly full-suite-on-Postgres run. The hand-curated 9-path allowlist already
   leaked once (#206).
