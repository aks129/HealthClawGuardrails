# Recipe: HealthClaw guardrails in front of Medplum

**Goal:** put the HealthClaw guardrail stack — PHI redaction, immutable audit,
step-up write authorization, human-in-the-loop, tenant isolation — between *any*
AI agent and a **Medplum** FHIR server, without changing Medplum.

**Why:** Medplum's own MCP server exposes near-raw FHIR CRUD to an LLM. That's
powerful, but there's no redaction on reads, no per-write step-up, and no
human-confirmation gate for agent-initiated writes. HealthClaw is the drop-in
layer that adds exactly those, in front of the Medplum you already run.

```text
AI agent ──▶ HealthClaw MCP ──▶ HealthClaw FHIR facade ──▶ Medplum FHIR R4
                                   │                          (your project)
                                   ├─ PHI redaction (reads)
                                   ├─ AuditEvent (every access)
                                   ├─ step-up token (writes)
                                   ├─ human-confirm (clinical writes)
                                   └─ tenant isolation
```

Every response from Medplum is redacted, audited, and URL-rewritten before it
reaches the agent; every write is validated and gated *before* it touches Medplum.

## What already exists

- `r6/fhir_proxy.py` → `MedplumProxy` (OAuth2 client-credentials, token cache
  in Redis with in-process fallback). `get_proxy()` builds it from env.
- The FHIR facade (`r6/routes.py`) applies `apply_redaction()` + `record_audit_event()`
  on top of every proxied read, and enforces step-up + human-confirm on writes —
  the same guardrails whether the data is local or Medplum-backed.
- **Proof:** `tests/test_medplum_in_front.py` exercises a real `MedplumProxy`
  (only the HTTP + token mocked) through the facade and asserts a Medplum-returned
  Patient comes back with name/SSN/phone/address/emergency-contact redacted, an
  AuditEvent recorded, and an unauthenticated write blocked before any Medplum call.

## Configure (env vars on the HealthClaw Flask service)

```bash
# Point the upstream proxy at your Medplum project (FHIR_UPSTREAM_URL must be empty)
MEDPLUM_BASE_URL=https://api.medplum.com/fhir/R4
MEDPLUM_CLIENT_ID=<your medplum client id>
MEDPLUM_CLIENT_SECRET=<your medplum client secret>

# Guardrail config (production)
READ_AUTH_ENABLED=true
INTERNAL_TOKEN_MINT_SECRET=<random>
STEP_UP_SECRET=<random 32+ bytes>
SESSION_SECRET=<random>
PUBLIC_TENANTS=            # leave empty for a real (non-demo) deployment
FHIR_UPSTREAM_ALLOWED_HOSTS=api.medplum.com   # SSRF allowlist
REDIS_URL=<optional; enables cross-worker token cache>
```

(Self-hosted Medplum: set `MEDPLUM_BASE_URL` to your instance's FHIR base and
adjust the token endpoint if you don't use `api.medplum.com`.)

## Run the live smoke

With a HealthClaw instance configured as above and reachable at `$BASE`:

```bash
python scripts/smoke_medplum.py --base-url $BASE --tenant-id <tenant> \
    --step-up-token <token>
```

It (1) creates a Patient in Medplum *through* HealthClaw (step-up +
`X-Human-Confirmed`), (2) reads it back and confirms the name/identifier are
redacted, and (3) fetches the AuditEvent proving the access was logged. It prints
PASS/FAIL per guardrail. See `scripts/smoke_medplum.py` for details.

## What a Medplum team gets from this

- Their FHIR server, unchanged, now safe to expose to agents.
- A guardrail layer that is FHIR-server-agnostic (works the same in front of
  HAPI, Aidbox, Google Cloud Healthcare) — so it's not a Medplum lock-in.
- A repeatable conformance test they can run against their own instance.

## Honest limits (see the main Known Limitations)

Redaction is HIPAA Safe-Harbor-style field redaction (demographics), not Expert
Determination; validation is structural, not full StructureDefinition/terminology
conformance. The guardrail *contract* (redact + audit + step-up + human-confirm +
tenant isolation) is what's demonstrated here; Grade-A error fidelity currently
applies to local-store search, while proxied-search behavior remains
upstream-dependent. Production de-id/validation rigor is on the roadmap.
