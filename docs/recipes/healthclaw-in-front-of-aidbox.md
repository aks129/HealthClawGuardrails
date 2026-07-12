# Recipe: HealthClaw guardrails in front of Aidbox (Health Samurai)

**Goal:** put the HealthClaw guardrail stack — PHI redaction, immutable audit,
step-up write authorization, human-in-the-loop, tenant isolation — between *any*
AI agent and an **Aidbox** FHIR server, without changing Aidbox.

**Why:** Aidbox now ships an [MCP module](https://www.health-samurai.io/docs/aidbox/modules/other-modules/mcp)
(alpha) that exposes FHIR CRUD tools to an LLM. That's powerful, and it's exactly
the surface a guardrail layer is for: today that module has no redaction on
reads, no per-write step-up, no human-confirmation gate for agent-initiated
writes, and no write-rate-limiting. HealthClaw is the drop-in layer that adds
precisely those, in front of the Aidbox you already run — the guardrails the MCP
module doesn't (yet) provide.

```text
AI agent ──▶ HealthClaw MCP ──▶ HealthClaw FHIR facade ──▶ Aidbox FHIR R4
                                   │                          (your box)
                                   ├─ PHI redaction (reads)
                                   ├─ AuditEvent (every access)
                                   ├─ step-up token (writes)
                                   ├─ human-confirm (clinical writes)
                                   └─ tenant isolation
```

Every response from Aidbox is redacted, audited, and URL-rewritten before it
reaches the agent; every write is validated and gated *before* it touches Aidbox.

## What already exists

- HealthClaw's upstream proxy (`r6/fhir_proxy.py`) is **FHIR-server-agnostic** —
  it already works in front of HAPI, Medplum, Google Cloud Healthcare, and Aidbox.
  Point it at any FHIR R4 base with `FHIR_UPSTREAM_URL`.
- The FHIR facade (`r6/routes.py`) applies `apply_redaction()` + `record_audit_event()`
  on every proxied read, and enforces step-up + human-confirm on writes.
- Errors from the upstream are surfaced as sanitized `OperationOutcome`s (not
  collapsed into empty bundles) — so an agent can tell "your query was rejected"
  from "no data," which matters more in front of a raw CRUD MCP module.

## Configure (env vars on the HealthClaw Flask service)

```bash
# Point the upstream proxy at your Aidbox instance's FHIR base
FHIR_UPSTREAM_URL=https://<your-box>.aidbox.app/fhir
FHIR_UPSTREAM_ALLOWED_HOSTS=<your-box>.aidbox.app   # SSRF allowlist

# Aidbox auth — Basic (client id/secret) or a bearer token, per your box's setup.
# Basic:
FHIR_UPSTREAM_AUTH=basic
FHIR_UPSTREAM_CLIENT_ID=<aidbox client id>
FHIR_UPSTREAM_CLIENT_SECRET=<aidbox client secret>

# Guardrail config (production)
READ_AUTH_ENABLED=true
INTERNAL_TOKEN_MINT_SECRET=<random>
STEP_UP_SECRET=<random 32+ bytes>
SESSION_SECRET=<random>
PUBLIC_TENANTS=            # leave empty for a real (non-demo) deployment
REDIS_URL=<optional; enables cross-worker token cache>
```

(Aidbox Cloud vs self-hosted: set `FHIR_UPSTREAM_URL` to your box's FHIR base.
Match `FHIR_UPSTREAM_AUTH` to how your box authenticates — Basic access policy,
or a pre-issued bearer.)

## Prove the guardrails hold

Run the conformance harness against the HealthClaw instance sitting in front of
Aidbox — it grades all six properties A–F, so you can *show* (not assert) that
redaction, audit, step-up, human-in-the-loop, tenant isolation, and disclaimers
are enforced on the path to your box:

```bash
curl "$BASE/r6/fhir/\$conformance?format=text"
```

## What an Aidbox / Health Samurai team gets from this

- Their Aidbox MCP module, unchanged, now safe to point an agent at: reads are
  redacted, every access is an AuditEvent, and agent writes are gated.
- A guardrail layer that is FHIR-server-agnostic — no Aidbox lock-in; the same
  stack runs in front of any FHIR R4 server.
- A repeatable, runnable conformance test they (or their customers) can run
  against their own instance — "verifiable, not marketing."

## Honest limits (see the main Known Limitations)

Redaction is HIPAA Safe-Harbor-style field redaction (demographics), not Expert
Determination; validation is structural, not full StructureDefinition/terminology
conformance. The guardrail *contract* (redact + audit + step-up + human-confirm +
tenant isolation) is what's demonstrated here — production de-id/validation rigor
is on the roadmap.
