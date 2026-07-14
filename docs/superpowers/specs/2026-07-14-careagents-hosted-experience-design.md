# CareAgents — the hosted experience layer for HealthClaw

**One line:** careagents.cloud lets anyone spin up a personal health agent in
under a minute — no OpenClaw, no MCP client, no developer setup — with every
read and action flowing through the HealthClaw guardrail layer.

Decision log written under the session /goal directive ("proceed to a working
product that has taste and delight and works"); approaches were weighed inline
and decisions recorded here rather than blocking on Q&A.

## Why this exists

HealthClaw proves the guardrail contract (Grade A, 7/7 properties) but every
current way to *use* it assumes a technical user: an MCP client, a Telegram
bot, curl. CareAgents is the consumer front door: the "full app" experience
where the guardrails are felt as *product trust*, not read as documentation.
It is also the roadmap's "Consumer onboarding" item made real.

## Approaches considered

1. **Proxy to the hosted OpenClaw gateway on the VPS.** Rejected: the explicit
   requirement is spinning up an agent *without* OpenClaw; the gateway is
   Gene's personal persona brain, single-tenant by design.
2. **Build the experience into the HealthClaw Flask app.** Rejected: muddies
   the OSS guardrail reference with a consumer product; careagents.cloud is a
   separate brand and host; the guardrail layer must stay a clean dependency
   (we consume it exactly the way we tell integrators to).
3. **Self-contained careagents app (CHOSEN).** A small Flask service in this
   repo (`careagents/`), deployed on the existing VPS behind the existing
   nginx at careagents.cloud. It runs its own server-side agent loop (LLM with
   tool use) whose only data path is HealthClaw prod's HTTP API — redaction,
   audit, step-up, tenant isolation, and the forms rail all inherited, never
   reimplemented. This is dogfooding: careagents is the first "HealthClaw in
   front of a real product" integration.

## The experience (v1)

1. **Landing** — warm, consumer-grade page (not a dev tool): "Meet your care
   agent." Live trust strip: the Grade A conformance badge fetched from prod,
   plus "every access is audited" and "you approve every action."
2. **Setup (~60 seconds, zero jargon)** — one screen: name your agent, pick a
   voice (three curated personas: **Calm Guide**, **Straight Shooter**,
   **Sunny Coach** — same safety posture, different tone), press Start.
   Behind the scenes: a fresh private tenant `ca-<10hex>` is created on
   HealthClaw prod, seeded with clearly-labeled sample records; a signed
   session cookie binds the browser to that tenant. A "Connect my real
   records" card is present but marked "coming soon" in v1 (the Fasten flow is
   the phase-2 wire-up).
3. **Chat** — streaming conversation with the agent. Tool activity renders as
   friendly chips ("Reading your labs — redacted view", "Checking care gaps").
   Suggested starters: *What do my labs say?* / *Any screenings I'm due for?*
   / *Fill out my intake form for a new doctor.*
4. **The forms-rail moment** — when the agent proposes the intake form, the
   chat shows a **Review & approve** card. The review page is served through
   careagents (credential-injecting proxy — see Security), the user confirms
   each medication/allergy (NKA never inferred; 422 fail-closed inherited),
   careagents then calls the out-of-band confirm, polls the action, and drops
   the **signed PDF link** back into the chat. End-to-end delight: ask →
   review → provenance-stamped PDF.
5. **Keep my agent** — "Save your agent" produces a magic re-entry link
   (signed token in the URL; no account, no password, no email required in
   v1). RESEND is available for email magic links in phase 2.

## Architecture

```
Browser ── careagents.cloud (nginx, TLS)
              │  location /  → 127.0.0.1:8600 (gunicorn, careagents Flask app)
              │  /gateway/ /telegram/ /hermes/ → untouched (existing services)
              ▼
   careagents app (this repo, careagents/)
      ├─ session.py    signed-cookie sessions (itsdangerous via flask secret)
      ├─ personas.py   3 persona system prompts, one safety core
      ├─ llm.py        provider adapter: Anthropic SDK when ANTHROPIC_API_KEY
      │                is set (preferred, claude-sonnet-5); OpenAI-compatible
      │                raw-HTTP fallback (OPENAI_API_KEY) so v1 works today
      ├─ agent.py      server-side tool loop, SSE streaming to the browser
      ├─ healthclaw.py HealthClaw prod HTTP client (the ONLY data path)
      └─ app.py        routes: / /setup /chat /api/chat(SSE) /review/<id> /magic/<t>
              │
              ▼  HTTPS, tenant-scoped headers
   app.healthclaw.io (guardrail layer — unchanged)
      mint token (X-Internal-Secret) · seed · search · $interpret ·
      $care-gaps · actions propose/commit/confirm/status · review page ·
      signed PDF delivery
```

### Agent tools (6, all HealthClaw HTTP)

| tool | HealthClaw call | notes |
|---|---|---|
| `get_health_summary` | Patient + Condition + MedicationRequest + AllergyIntolerance searches | redacted by the layer |
| `get_labs` | Observation search + `$interpret` | plain-language ranges |
| `get_care_gaps` | `Patient/$care-gaps` | USPSTF/ACIP/ADA |
| `search_records` | typed FHIR search | patient/code/status only |
| `start_intake_form` | actions propose → commit (kind `form-fill`) | returns careagents review URL |
| `check_form_status` | action status | surfaces `delivery_link` when completed |

The loop caps at 6 tool rounds per turn; every tool result is already redacted
server-side by HealthClaw before careagents (or the model) sees it.

### Session & tenant model

- Tenant id: `ca-<10 hex>`, created at setup, **non-public** on prod (so
  read-auth applies to everyone else; only the careagents backend can mint its
  tokens, via `X-Internal-Secret`).
- Signed cookie payload: `{tenant, agent_name, persona}`; magic link = the
  same payload signed with a 90-day expiry.
- Step-up tokens are minted server-side per request-burst and cached until
  ~30s before TTL; the browser never sees a token or the mint secret.

### Review-page proxy (the one subtle piece)

Prod's `GET/POST /r6/actions/<id>/review` authenticates via headers, which a
browser link can't send. careagents exposes `/review/<action_id>`:
server-fetches the prod review page with injected tenant+token headers,
rewrites the form-post target to careagents, and relays the POST. The proxy
refuses any action id whose action does not belong to the session's tenant
(checked against action status before relaying). After a successful review
POST, careagents calls `POST /r6/actions/<id>/confirm` (out-of-band, exactly
like the Telegram approver), then polls status and posts the signed PDF link
into the chat. The signed delivery link itself is served by prod directly —
the URL signature is the credential; no proxy needed.

## Security posture

- **The guardrails stay server-side on HealthClaw** — careagents adds UX, not
  policy. Nothing careagents does can bypass redaction/audit/step-up/HITL.
- The mint secret and LLM keys live only in the VPS service environment.
- Session cookies signed with `CARE_SESSION_SECRET` (fail-closed: app refuses
  to boot in production without it, mirroring HealthClaw's posture).
- Per-session rate limit on `/api/chat` (token bucket, 20 turns/10 min) to
  bound LLM spend from an unauthenticated public site.
- System prompt encodes the GEMINI.md calling contract: decision support not
  medical advice; emergencies → 911; NKA never asserted by the agent; writes
  and actions are proposals the human approves out-of-band.
- Sample data is labeled "sample records" in the UI until real records are
  connected. No PHI in careagents logs (request logs carry tenant + route
  only).

## What "works end to end" means (acceptance)

1. `careagents.cloud` serves the landing page over TLS.
2. Setup creates+seeds a fresh tenant and lands in chat in ≤60s.
3. "What do my labs say?" streams an answer with a labs tool chip, on live
   prod data (sample tenant).
4. "Fill out my intake form" → Review & approve card → per-item review (an
   attempt to remove the allergy without NKA is rejected 422) → confirm →
   **signed PDF link opens** from a clean browser.
5. Guardrail strip shows the live Grade A badge.
6. Repo suite + ruff green (careagents unit tests mock all HTTP; no network in
   CI). A `scripts/careagents_smoke.py` drives 1–4 against the live site.

## Explicitly out of scope for v1 (phase 2 backlog)

Real-records connect via Fasten from careagents; email magic links (Resend);
multiple agents per person; caregiver/consent model; billing; comms rail
(calls/SMS — provider-blocked anyway); Ollama/local-model option.
