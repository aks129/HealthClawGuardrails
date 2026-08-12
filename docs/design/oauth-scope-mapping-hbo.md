# Design: HBO OAuth scope authorization for HealthClaw agents

**Status:** Proposal for the Health Bank One technical discussion.
**Goal:** an AI agent's authority to touch a member's records becomes **externally
verifiable** — HBO issues the scoped credential, HealthClaw enforces it at every
tool call and audits the result. Bo's "Open Banking for health": HBO owns the
trust rail, HealthClaw owns the safety rail.

## Where HealthClaw is today

HealthClaw already speaks the vocabulary — it's currently its *own* OAuth authority:

- `r6/oauth.py` — SMART-on-FHIR v2 scopes (`patient/*.read`, `patient/*.write`,
  resource-scoped variants), OAuth 2.1 + PKCE authorize/token, dynamic client
  registration, `.well-known/smart-configuration`, and `validate_bearer_token`.
- **Tool tiers:** the 26 MCP tools are already typed `read` | `write`
  (`ToolTier` in `tools.ts`); writes additionally require an HMAC step-up token
  and an explicit human-confirmation header (HTTP 428 otherwise).
- **Guardrails** run regardless of caller: PHI redaction on reads, immutable
  AuditEvent, tenant isolation, medical disclaimers (all provable via
  `GET /r6/fhir/$conformance`).

**The gap:** authorization today is *self-asserted* — a tenant header + a
HealthClaw-minted token. Nothing a third party can independently verify.

## Target: HealthClaw as a relying party on HBO

Flip the issuer. HBO's OAuth/OIDC service becomes a **trusted external issuer**;
HealthClaw validates HBO-issued credentials and maps their scopes onto its
existing tool-tier + guardrail model.

```text
Member ──auth──▶ HBO OAuth/OIDC ──scoped token/cert──▶ Agent ──Bearer──▶ HealthClaw
                 (verifiable issuer)                            │  1. verify (JWKS/introspect)
                                                                │  2. scope → allowed tools
                                                                │  3. guardrails enforce HOW
                                                                └▶ FHIR / MCP tools
```

The two-layer split is the point:

- **OAuth scope authorizes WHICH** tools/resources an agent may touch (from HBO,
  verifiable).
- **HealthClaw guardrails enforce HOW** — redaction, audit, step-up on writes,
  human-in-the-loop, tenant isolation. A broad scope still can't bypass the
  guardrails; a narrow scope further constrains the tool surface.

## Scope → tool authorization matrix

Mapping HBO/SMART scopes onto HealthClaw's read/write tiers:

| HBO / SMART scope | HealthClaw authorization |
| --- | --- |
| `openid` / identity | establishes the verifiable subject; no data access |
| `patient/*.read` (or `patient/*.rs`) | all **read**-tier tools (`fhir_search`, `fhir_read`, `search`/`fetch`, `fhir_interpret_labs`, `questionnaire_populate`, `curatr_evaluate`, …) |
| `patient/<Resource>.read` (e.g. `patient/Observation.rs`) | read-tier tools **restricted to that resource type** |
| `patient/*.write` (or `.cruds`) | **write**-tier tools — but still gated by HealthClaw step-up + human-in-the-loop |
| (absent) | nothing beyond public/synthetic demo data |

Enforcement point: the MCP server (and the FHIR facade's `authenticate_read` /
step-up path) reads the validated scope set from the HBO token and intersects it
with the tool's declared tier + resource type before dispatch.

## Verifiable audit / provenance

Today an AuditEvent's actor is a self-asserted `agent_id`. With HBO OAuth, the
audit records the **verified OAuth subject + client + scopes** — so the trail
answers "which agent, authorized by whom, under what scope" with a credential a
third party (or a regulator) can check. This is the dispute-resolution substrate
Bo has described: every action traces to a verifiable authorization.

## Migration path (non-breaking)

1. **Today:** HMAC step-up + tenant header (self-asserted). Unchanged.
2. **Phase 1 — accept HBO bearer:** HealthClaw validates HBO-issued tokens
   (JWT via HBO JWKS, or opaque + introspection) as an *additional* accepted
   credential on the FHIR facade + MCP. Scopes map per the matrix. Step-up +
   HITL still gate writes.
3. **Phase 2 — scope-tight tools:** per-resource scope enforcement on the tool
   surface; AuditEvent carries the verified subject/scopes; revocation honored
   via HBO introspection.
4. **Phase 3 — agent certificates:** if HBO issues per-agent verifiable
   credentials, HealthClaw validates the cert chain and binds scope to the
   agent identity end to end.

## Open questions for HBO (the technical thread)

1. **Token format** — JWT (self-contained, verified against a JWKS endpoint) or
   opaque (requires a token-introspection endpoint)? What's the JWKS / introspect URL?
2. **Scope vocabulary** — SMART v2 (`patient/Observation.rs`), SMART v1
   (`patient/Observation.read`), or an HBO-specific scheme? We support v1/v2 today.
3. **"Certificates third parties can verify"** — signed JWT with HBO as `iss`,
   mTLS client certificates, or verifiable-credential/DID? This shapes how
   HealthClaw validates and how the audit records provenance.
4. **Agent identity** — per-agent client credentials (client_credentials or a
   software statement), or purely patient-delegated authorization-code flows?
5. **Revocation & freshness** — introspection for real-time scope/revocation
   checks, or short-lived tokens + refresh? What TTLs?
6. **Consent granularity** — does HBO express resource-type / data-category
   consent in the scope, or is it all-or-nothing patient access?

## Why this is the right pairing

HealthClaw already enforces the *how* (redaction, audit, step-up, HITL) and
already speaks SMART scopes — it just needs a trustworthy *who/what*. HBO is a
digital-identity company issuing exactly that. Neither piece is redundant: HBO
makes authorization verifiable; HealthClaw makes the authorized access safe. The
conformance harness proves the safety half holds on HBO-backed data today.
