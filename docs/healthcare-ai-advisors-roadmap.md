# The Healthcare AI Advisors System — Setup + Roadmap

How HealthClaw Guardrails, CareAgents, and SmartHealthConnect compose into one
guardrailed advisor system inside Claude — what works today, and what it takes
to finish it.

**Status:** drafted 2026-07-19. Part 1 is a verified inventory; Part 2 is
runnable today; Part 3 is the build plan. Items marked ⚠️ are gaps I found
while writing this, not speculation.

---

## 0. The system in one picture

Three repos, one contract: **the engine owns policy, the surfaces own
experience.** No surface ever touches FHIR directly.

```text
                        ┌──────────────── Claude ────────────────┐
                        │  MCP connectors + MCP Apps (views)      │
                        │  the advisor's tool surface             │
                        └────────┬───────────────────┬────────────┘
                                 │                   │
              ┌──────────────────┘                   └──────────────┐
              │                                                     │
    ┌─────────▼──────────┐                              ┌───────────▼─────────┐
    │ SmartHealthConnect │  surface                     │     CareAgents      │  surface
    │  (Liara AI Health) │  patient skills + 7 MCP-App  │  (careagents/)      │  consumer app
    │  v1.2.0            │  views + React client        │  accounts, agents,  │
    │  role: surface     │                              │  web/Telegram/iMsg  │
    └─────────┬──────────┘                              └───────────┬─────────┘
              │                                                     │
              │            HTTP / MCP only — never direct FHIR      │
              └──────────────────────┬──────────────────────────────┘
                                     │
                      ┌──────────────▼───────────────┐
                      │   HealthClaw Guardrails      │  ENGINE
                      │   role: engine               │
                      │  • FHIR store + facade       │
                      │  • PHI redaction (Safe Harbor)│
                      │  • immutable audit           │
                      │  • step-up auth              │
                      │  • tenant isolation          │
                      │  • Compiled Truth            │
                      │  • action rail (human gate)  │
                      └──────────────┬───────────────┘
                                     │
        ┌────────────┬───────────────┼──────────────┬──────────────┐
     Fasten      Wearables      SMART Health     Medent        HealthEx /
   (verified   (Open Wearables:   Links/Cards   (direct EMR)  Health Bank One
    provider)   Apple, Oura,       (r6/shc)                    (r6/shc callbacks)
                Whoop, Garmin…)
```

The engine/surface split is **declared, not just conventional**:
`.health-context.yaml` in each repo (`role: engine` / `role: surface`) names the
counterpart. That file is the thing to update first if the topology changes.

### Why this shape matters

The advisor is only as trustworthy as its narrowest gate. Because every surface
reaches PHI through HealthClaw's HTTP/MCP API, the guardrails cannot be bypassed
by a chatty model, a compromised surface, or a clever prompt. Redaction,
audit, and the human approval gate run **server-side, once** — adding a fourth
surface adds zero new policy surface area.

---

## 1. What's actually live today

Verified against the repos, not aspirational.

| Layer | Component | State |
| --- | --- | --- |
| Engine | HealthClaw Flask app (`app.healthclaw.io`, Railway) | ✅ live, auto-deploys on `main` |
| Engine | MCP server (Streamable HTTP, Railway) | ✅ live, **manual deploy only** |
| Engine | Guardrail conformance, graded A–F | ✅ Grade A, CI-gated |
| Engine | Action rail + out-of-band human gate | ✅ forms rail end-to-end |
| Engine | Compiled Truth (`fhir_compiled_truth`) | ✅ |
| Surface | SmartHealthConnect v1.2.0 — 6 patient skills | ✅ built |
| Surface | SHC MCP App — 7 views | ✅ built, ⚠️ not submitted |
| Surface | SHC MCP server — ~30 tools | ✅ built, ⚠️ contract drift (§4.2) |
| Surface | CareAgents (`careagents.cloud`) | ✅ live, **independent deploy** |
| Surface | CareAgents connector marketplace | ✅ live (7 sources) |
| Surface | CareAgents iMessage | 🟡 code shipped, blocked on TCC grants |
| Claude | HealthClaw MCP in registry (`server.json` v1.8.0) | ✅ published |
| Claude | SmartHealthConnect MCP App in directory | ⚠️ blocked (§4.1) |

**The advisor roster today.** Six patient skills (`healthy-habits`,
`care-completion`, `medication-refills`, `diet-exercise`, `kids-health`,
`research-monitor`) plus three CareAgents personas (Calm Guide, Straight
Shooter, Sunny Coach). That is the "advisors" layer — specialized agents over
one shared, guarded record.

**Connector tiers in CareAgents** (`careagents/connectors.py`): `live` — sample,
Fasten (verified provider), Apple Health + wearables. `import` — SMART Health
Link, upload. `soon` — HealthEx, Health Bank One. Tiers are **config-gated**, so
a source only advertises as live where its flow is genuinely wired. Keep that
discipline; it is why the marketplace doesn't lie.

---

## 2. Setup — stand it up in Claude today

This is the working path with what exists now. ~30 minutes.

### 2.1 Connect the engine to Claude

HealthClaw is already in the MCP registry as a remote Streamable HTTP server, so
no local install is needed.

- **URL:** the `remotes[0].url` in [`server.json`](../server.json)
- **`X-Tenant-Id`** — the public demo tenant `desktop-demo` works with no
  credentials. Use it to verify the connection before touching real data.
- **`X-Step-Up-Token`** — a tenant-bound HMAC. Required for write-tier tools and
  for reads on any non-public tenant.

> **Never paste a step-up token into a chat, an issue, or a prompt.** Tokens are
> minted server-side only (`HEALTHCLAW_MINT_SECRET`). CareAgents mints them for
> its own tenants; nothing client-side should ever hold the mint secret.

Verify with a read-only call first — `guardrail_conformance` is ideal: it
returns the live grade and proves the connection without touching PHI.

### 2.2 Get records into a tenant

Pick the path that matches the source:

| Source | Path | Notes |
| --- | --- | --- |
| Verified provider | CareAgents → Fasten | Runs on HealthClaw's `/connect/<tenant>` Stitch page |
| Apple Health / wearables | Open Wearables sidecar | Needs `CARE_WEARABLES_ENABLED` + sidecar wired |
| SMART Health Link / Card | `r6/shc` ingest | Import tier |
| Direct EMR (Medent) | `r6/shc/medent/callback` | OAuth callback + bundle ingest |
| Health Bank One | `r6/shc/hbo/callback` | Callback exists; CareAgents tile still `soon` |
| Demo | sample records | Synthetic only — use for every rehearsal |

### 2.3 Add the patient surface

SmartHealthConnect's MCP server proxies into the engine. Configure:

```bash
HEALTHCLAW_MCP_URL=https://<your-mcp-host>/mcp/rpc   # or http://localhost:3001/mcp/rpc
HEALTHCLAW_TENANT_ID=<tenant>
```

The **rule that makes this safe**: a skill making a resource-specific claim to a
patient must call `get_compiled_truth` first — it returns current redacted state
plus `curation_state`, `quality_score`, and the Provenance timeline. An advisor
that asserts "your A1c is 7.2" without a Provenance trail is exactly the failure
mode this system exists to prevent. Every skill's `SKILL.md` states the rule.

### 2.4 Spin up an advisor

Either surface, same engine:

- **CareAgents** (`careagents.cloud`) — sign in with a passkey, connect records,
  create an agent with a persona, chat on web/Telegram. Non-developer path.
- **Claude directly** — talk to the HealthClaw MCP tools with the SHC skills
  loaded. Developer path, and the one to use for the HIMSS/partner demos.

### 2.5 Prove the guardrails before you trust it

Do this once per deployment, and before any live demo:

1. `guardrail_conformance` → confirm **Grade A**.
2. Read a resource → confirm an AuditEvent was emitted and its `detail` is
   PHI-free.
3. Attempt a clinical write with a valid step-up token → confirm it returns
   **202 (submitted)**, not executed.
4. Confirm execution requires the separate approval endpoint. **No agent
   toolchain can approve its own action** — if it can, stop and fix that first.

---

## 3. Phased roadmap

Ordered by dependency, not date. Each phase has an exit gate.

### Phase 0 — Unblock what's already built ⚡ *highest value per hour*

Three finished things aren't reachable by users. Fix those before building more.

- **Fix the SHC MCP App manifest misattribution** (§4.1) — blocks directory
  submission today. ([SmartHealthConnect#10](https://github.com/aks129/SmartHealthConnect/issues/10))
- **Land iMessage** — code is shipped; needs two GUI TCC grants on the Mac mini
  (Full Disk Access + Automation→Messages), then load the launch agent. ([#136](../../issues/136), [#137](../../issues/137))
- **Reconcile the SHC tool surface with the compiled-truth rule** (§4.2). ([SmartHealthConnect#11](https://github.com/aks129/SmartHealthConnect/issues/11))

**Exit gate:** SHC App submittable, iMessage answering, contract drift closed.

### Phase 1 — One patient identity across surfaces

> Tracked in **[#157](../../issues/157)** (epic).

Today there are three identity models: HealthClaw step-up tokens, CareAgents
passkeys + email codes, SmartHealthConnect Passport sessions. A person is a
different subject in each, so an advisor cannot follow them across surfaces.

- Decide the canonical subject (recommendation: **the HealthClaw tenant**, since
  it already scopes every resource and audit row).
- Map CareAgents accounts and SHC sessions onto it.
- Keep biometrics on-device; passkeys never leave the client.

**Exit gate:** one person, one tenant, reachable from web + Claude + iMessage
with a single connection story. This is the prerequisite for a *system* rather
than three apps sharing a backend.

### Phase 2 — The advisor roster becomes a team

> Tracked in **[#158](../../issues/158)** (router), **[#159](../../issues/159)** (shared memory), **[#160](../../issues/160)** (escalation).

Nine advisors exist as isolated skills/personas. Make them compose.

- A **router**: which advisor should answer this? (refills → `medication-refills`;
  a kid's fever → `kids-health`).
- **Shared memory** across advisors, scoped to the tenant, PHI-free in logs.
- **Escalation**: an advisor that hits an action needing approval hands off to
  the human gate with a review card, rather than dead-ending.
- Every advisor inherits the disclaimer + red-flag emergency screen. Non-optional.

**Exit gate:** a single question routes to the right advisor, gets a
Provenance-backed answer, and escalates cleanly when it needs a human.

### Phase 3 — Advisors that act, not just answer

> Tracked in **[#161](../../issues/161)** (comms rail, epic), **[#162](../../issues/162)** (refills), **[#163](../../issues/163)** (appointments).

The action rail exists and the forms rail proves it end-to-end. Extend it.

- Comms rail (calls/SMS to allowlisted, patient-registered contacts) — already
  "Now" on the main roadmap.
- Refill requests through the rail rather than a bare tool call.
- Appointment prep + booking.
- **Every one** behind propose → 202 → out-of-band approval → audit →
  reconciliation. No exceptions, no new gates invented per-capability.

**Exit gate:** an advisor completes a real-world task where the human approved
exactly once, and the audit trail reconstructs the whole chain.

### Phase 4 — Distribution

> Tracked in **[#164](../../issues/164)** (epic).

- SHC MCP App in the directory; HealthClaw already in the MCP registry.
- Partner path: Aidbox/Health Samurai as the store, HealthClaw as the guard,
  CareAgents + SHC as advisors (the joint blog post covers this narrative).
- Consumer listings + the HIMSS Keystone webinar (Aug 18) as the forcing function.

**Exit gate:** someone who has never met you installs it and connects a real record.

---

## 4. Gaps found while writing this

### 4.1 ⚠️ SHC MCP App manifest misattributes the project to Anthropic

> [SmartHealthConnect#10](https://github.com/aks129/SmartHealthConnect/issues/10)

`mcp-app/manifest.json` points `author.url`, `homepage`, `repository.url`, and
the **privacy policy URL** at `github.com/anthropics/SmartHealthConnect`. The
real repo is `github.com/aks129/SmartHealthConnect`.

Three separate problems: it misattributes authorship to Anthropic; the privacy
policy link **404s**, which for a health app is a directory-review failure and a
trust problem; and it would likely be rejected on review. Low effort, high
consequence — fix before any submission.

### 4.2 ⚠️ SHC MCP server tool surface vs. the compiled-truth rule

> [SmartHealthConnect#11](https://github.com/aks129/SmartHealthConnect/issues/11)

`mcp-server/src/index.ts` exposes ~30 tools including `get_conditions`,
`get_medications`, `get_vitals`, `get_allergies`. The declared contract says
patient skills route resource-specific claims through `get_compiled_truth` and
"never read FHIR directly."

I did **not** verify whether these proxy to the engine or read independently —
that's the open question. If they bypass, the guarantee is weaker than the
contract claims. Two honest resolutions: route them through the engine, or
narrow the contract's wording to match reality. The version-bump ritual makes
§2 of each retrospective the natural place to enforce whichever you pick.

### 4.3 ⚠️ Playwright e2e is red on `main`

> [#154](../../issues/154)

Every recent `main` run fails, producing no report — an environment/setup
failure, not a code regression. It's been failing across at least five commits.
The cost is that e2e currently provides **no signal** on any PR, so a real
break would look identical to today. Worth fixing before the demo push.

### 4.4 Deployment asymmetry — easy to get wrong

> [#155](../../issues/155)

Three different deploy models, and two are manual:

- HealthClaw Flask app — auto-deploys on `main` (Railway)
- MCP server — **manual** staging-dir deploy
- CareAgents — **independent** VPS deploy; shared prod host, needs explicit
  authorization

A change to MCP tools is live in the repo but not in Claude until the manual
deploy runs. That gap has already bitten this project once.

---

## 5. The invariants none of this may break

Any advisor, any surface, any phase:

- Redaction, audit, step-up, and the human gate run **server-side** — a surface
  cannot weaken them.
- Every FHIR access emits an AuditEvent; audit `detail` stays **PHI-free**.
- Clinical writes need out-of-band human confirmation via a separate endpoint.
- **"No known allergies" is never inferred** — only from explicit human attestation.
- CareAgents and SmartHealthConnect store **no PHI**.
- Conformance stays **Grade A** (CI-gated).
- Demo data is synthetic. Always.

---

## Related

- [docs/agent-task-guide.md](agent-task-guide.md) — **start here if you're picking up an issue**
- [ROADMAP.md](../ROADMAP.md) — the engine roadmap (Now/Next/Later)
- [CLAUDE.md](../CLAUDE.md) — engine architecture + invariants
- [docs/development.md](development.md) — contributor guide, deploy steps
- `.health-context.yaml` — engine/surface declarations (both repos)
