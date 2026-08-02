# Delivery Plan — HIMSS (Aug 18) + ICP (late Aug)

**Today:** Sat Aug 1, 2026 · **HIMSS webinar:** Tue Aug 18 (17 days) · **ICP webinar:** late Aug

**Goal:** a product a famous patient advocate can use unsupervised, be impressed by, and
tell people about. Not a demo that works when driven by its author.

Companion analysis: [2026-08-01-alignment-review.md](2026-08-01-alignment-review.md)

---

## The bet

**What we demo:** *"I connected my real records. My agent read them, told me something
true I didn't know, filled my new-patient intake form from them, I reviewed every line,
and here is the signed PDF — with an audit trail of every access."*

Every read-only competitor (ChatGPT Health, Claude+HealthEx, Perplexity) stops one step
before that PDF. That step is the whole story, and **it already works today**.

**What we are NOT betting on:** the pharmacy voice call. It is the better story, but it
sits behind Bland onboarding — an external dependency we do not control 17 days out.
Start the onboarding now; treat the call as a fast-follow that, if it clears, becomes the
ICP-webinar headline.

---

## Simplifications (chosen deliberately over the "proper" build)

| Instead of | We ship | Why |
|---|---|---|
| A terminology service / FHIR ValueSet resolver | A **static Python dict** of the top-N codes, sized by measurement | Zero infra, zero latency, trivially troubleshootable. A dict lookup cannot fail in production. |
| A new chat-history store + PHI-boundary renegotiation | **Reuse `ConversationMessage`** (`r6/command_center/models.py`) and the existing `GET`/`POST /command-center/api/conversations` | Already tenant-scoped, already on the HealthClaw side of the boundary, already covered by `purge_tenant`. Persistence becomes wiring, not architecture. |
| A document-understanding pipeline | **Add `DocumentReference` to the search enum** + surface existing text through redaction | The read path is already proven in the MCP server. |
| Sentry + metrics + dashboards | **One 6-hourly cron** running the existing `scripts/careagents_smoke.py`, opening a pinned issue on failure | The alerting channel a solo maintainer actually reads. |
| Racing Bland for a live call | **Polish the intake form** that works today | Value per day-of-risk is far higher, and it is the same "real action" claim. |

Rule for the next 17 days: **if a fix needs a new service, a new table, or a new
dependency, it is the wrong fix for this window.**

---

## Value gates — measure before building

Each Phase-1 item is gated on evidence. If the gate fails, we do not build it; we
re-plan. This is the discipline that keeps the window honest.

| Gate | Measurement | Build only if | If it fails |
|---|---|---|---|
| **G1 — Terminology** | On the real MEDENT tenant, count distinct codes and cumulative coverage (count-only query; no PHI values leave the engine) | A few hundred codes cover the clear majority of records | Long tail → labels won't fix the payoff; pivot to showing structured data (dates, values, status) instead of names |
| **G2 — Documents** | Sample what `DocumentReference` rows actually contain | They carry readable text or an inline attachment | Only external URLs → the tool cannot help; drop it and say so |
| **G3 — Persistence** | Confirm `recent_conversations` returns full text | Full text available | `to_dict` truncates at 500 chars — small fix, then proceed |
| **G4 — Retention loop** | Do we have a user who returned twice? | Yes | **No → do not build the care-note loop.** Retention features before retention are theater |

G4 is the one most likely to save us a week.

---

## Phase 0 — Close the exposure + measure (Aug 1-4)

Nothing user-visible. This is the difference between "we have guardrails" being true and
being marketing — and we are about to say it on a stage.

- [ ] **Kill `/demo/agent-loop`** — restrict to `is_public()` tenants + `X-Internal-Secret`, or disable in prod. *Live, unauthenticated, cross-tenant write + policy-delete.*
- [ ] **Bind the confirm credential** — `require_audience='action-approval'` + operation. Copy the Curatr pattern (`r6/routes.py:2613`).
- [ ] **Bound the agent loop** — trim history before each call, hard `return` after `MAX_TOOL_ROUNDS`, evict idle entries.
- [ ] **`pool_pre_ping=True` + `pool_recycle=300`** on the careagents engine.
- [ ] **Fix the two silent lies** — propagate `mail.send_code` failure; stop swallowing `confirm_action` errors.
- [ ] **Track + CI-build `deploy/careagents/Dockerfile`** (still untracked).
- [ ] **Fix stale `X-Human-Confirmed`** in `REVIEW_STANDARDS.md` / `development.md` / `SECURITY.md` — the review bot's constitution is wrong.
- [ ] **Run G1 + G2 + G3 measurements.**

**Exit:** prod has no unauthenticated write path; suite green; Grade A; measurements in hand.

---

## Phase 1 — Make the payoff land (Aug 5-11)

The through-line: a user who connects real records today gets amnesia, unreadable labels,
and unreadable documents. Same connection, three fixes, real product.

- [ ] **Terminology labels** (gated on G1) — static map, applied in `_summarize_bundle`. Unknown codes keep the honest `unreadable: True` fallback.
- [ ] **Chat persistence** (gated on G3) — write each turn to `ConversationMessage` via the existing endpoint; rehydrate on load; render prior turns in `chat.html`.
- [ ] **`get_documents` tool** (gated on G2).
- [ ] **Real first greeting** — replace the hardcoded string with actual findings ("I can see 3 conditions, 7 medications, 12 labs from *Provider*"). Kill "(sample data for now)" for real connections.
- [ ] **Copy honesty pass** — consent card says email-support-to-delete while Delete sits on the same page (needs `CONSENT_VERSION` bump).

**Exit:** connect real records → first screen says something true and useful → close the
tab → come back → the conversation is still there.

---

## Phase 2 — Demo-solid (Aug 12-16)

The bugs that make a live demo or an unsupervised advocate hit a wall.

- [x] **Passkey dead loop** — `/auth` redirects logged-in users away; enrollment unreachable after signup. (#241)
- [ ] **Replace `prompt()`/`alert()`** in wearable pick + Telegram/iMessage binding; style the conn-card buttons; differentiate destructive Delete. (#224)
- [ ] **CareAgents Playwright spec** — signup → sample connect → agent create → one chat turn. None exists today; all e2e targets the HealthClaw site. (#233)
- [x] **Prod synthetic monitor** — 6-hourly cron: smoke script + `$conformance` Grade A + `/healthz` + MCP `tools/list` → pinned issue on failure. (#244)
- [ ] **Intake-form polish** — the demo centerpiece: review card clarity, PDF quality, error states.
- [ ] **Ship what we merged** — see the ship gate below. (#258)

**Exit:** a stranger completes the full journey on a phone without help,
**against the deployed build** — not against `main`.

### The ship gate (added Aug 2, after we caught ourselves)

Merged is not shipped. The Flask engine auto-deploys on push to `main`;
**CareAgents and the MCP server do not.** On Aug 2 every CareAgents change
from Phase 0 and Phase 1 — durable chat history, the passkey fix, the daily
turn cap — was sitting on `main`, unshipped, while `prod_watch.py` reported
9/9 green. It was green because liveness, readiness, grade, and readability
are all satisfied by a months-old build. Nothing we monitor can see version
drift (#258, and #155 for the same problem on the MCP server).

So each phase now ends with a deploy, not a merge:

- Phase 2 exits only when the journey works on `careagents.cloud`, on a
  phone, on the build a stranger would actually reach.
- Phase 3's dry run is meaningless before that deploy lands, and the passkey
  cannot be exercised at all until DNS points at the deployed service —
  WebAuthn is bound to `CARE_RP_ID=careagents.cloud`.
- Any "it's done" claim in this plan means deployed and checked, or it is not
  a claim.

---

## Phase 3 — Freeze + rehearse (Aug 17-18)

- [ ] **Code freeze Aug 16 EOD.** Only demo-blocking fixes after.
- [ ] Full dry run on the real deployment, on the actual demo device and network.
- [ ] One advocate runs it unsupervised while we watch. Fix only what blocks them.
- [ ] Fallback plan: recorded backup of the intake-form flow if live connect fails on stage.

---

## Phase 4 — Advocates + ICP (Aug 19-31)

- [ ] Onboard 3-5 patient advocates on real records; instrument where they stall.
- [ ] **Bland, if cleared:** approve surface → allowlist + daily cap → reaper cron → dogfood ladder (own number → real pharmacy IVR → advocate scenario). This is the ICP headline if it lands.
- [ ] Publish the trust artifact: **"X actions completed, 100% human-approved, 0 safety incidents."**
- [ ] Care-note retention loop — **only if G4 passes.**

---

## Deliberately NOT doing before Aug 18

Naming these so they stop consuming attention:

- Open Wearables auth gap (upstream dependency, unbounded)
- Productizing the MEDENT SMART flow (weeks; Fasten already covers the demo)
- Alembic migration chain for careagents
- Multi-worker scale-out / shared history (blocked on a boundary decision; 1 worker × 16 threads covers 50 users)
- Retiring `X-Human-Confirmed` for direct clinical writes (real, but not demo-path — issue it)
- Full conformance probe expansion (strategic, not demo-critical — issue it)
- Appointment booking (deliberately post-webinar per spec v3)

---

## Vision + market checkpoints

Re-read before each webinar; these are the claims we can defend:

- **We are the only patient-owned agent that executes actions at any provider behind
  propose → human-approve → audit.** Read-only is the incumbent posture (ChatGPT Health
  nationwide Jul 23; Claude+HealthEx; Perplexity). Amazon acts only inside its own clinic.
- **We are the only open-source guardrail engine that grades live deployments.** Innovaccer
  HMCP is the nearest artifact — a spec plus a commercial gateway for health systems.
  *Our grade must be honest for this to hold* — hence Phase 0 and the probe-expansion issue.
- **Do not claim** speed-to-agent or connectivity breadth as moats. Incumbents beat us on
  both.
- **Existential thread:** if HMCP becomes the standard while we remain a project, the
  standard-setter thesis inverts. Keep the Bo Holland agent-identity collaboration and
  CARIN engagement warm through August.
