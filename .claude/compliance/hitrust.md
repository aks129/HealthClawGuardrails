# HITRUST CSF Compliance Gates

Gates for HITRUST Common Security Framework requirements relevant to this stack.
Focused on controls for healthcare AI systems: access control, session management,
tenant isolation, and data integrity. CI validates HT1–HT3 automatically.

---

## HT1 — Tenant Boundary Enforcement (HITRUST Control 01.a)
**CI: automated** | `compliance-gates` verifies cross-tenant access blocked

Every data operation is scoped to a single tenant. Tenant identity comes exclusively
from the `X-Tenant-ID` header — it MUST NOT be accepted from the request body, URL
params, or JWT claims in the current implementation.

**Enforcement:** All `R6Resource.query` calls filter by `tenant_id=tenant_id` where
`tenant_id = request.headers.get('X-Tenant-ID')`.

**Boundary rules:**
1. Missing `X-Tenant-ID` on write → 400 Bad Request
2. Cross-tenant read → 404 Not Found (not 403 — do not confirm existence)
3. `fhir_seed` populates only the requesting tenant's namespace
4. AuditEvents are tenant-scoped — one tenant cannot read another's audit trail

**Gate check matrix:**
| Operation | No Tenant Header | Wrong Tenant | Correct Tenant |
|-----------|-----------------|--------------|----------------|
| GET /{type}/{id} | 400 | 404 | 200 |
| POST /{type} | 400 | N/A | 201 |
| GET /AuditEvent | 400 | empty Bundle | tenant records |

**Prohibited patterns:**
```python
# BAD — tenant from body
tenant_id = request.json.get('tenant_id')

# GOOD — tenant from header only
tenant_id = request.headers.get('X-Tenant-ID')
if not tenant_id:
    return jsonify(make_outcome('error', 'required', 'X-Tenant-ID header required')), 400
```

---

## HT2 — Session and Token Lifecycle (HITRUST Control 01.d / 09.ab)
**CI: automated** | `compliance-gates` verifies expired tokens rejected

Step-up tokens have a 5-minute TTL and are tenant-bound. Expired or cross-tenant
tokens MUST be rejected with 401. Tokens are single-use-scoped (HMAC nonce prevents replay
within the TTL window is not enforced — see Known Limitations).

**Token structure:** `base64url({exp, tid, sub, nonce}).hmac_hex`
**Validation:** `r6/stepup.py:validate_step_up_token(token, tenant_id)` → `(bool, error_str)`

**Lifecycle controls:**
- Token TTL: 300 seconds (configurable via `DEFAULT_TOKEN_TTL_SECONDS`)
- Token scope: bound to `tenant_id` at generation time
- Token source: `POST /r6/fhir/internal/step-up-token` (requires `X-Tenant-ID`)
- MCP shortcut: `fhir_get_token` tool issues a token for the current session tenant

**Gate check:** Token issued for `tenant-A` MUST fail validation for `tenant-B`.
**Gate check:** Token with `exp` in the past MUST return 401.

**Known gap:** Nonce replay prevention within the 5-min window is not implemented.
Tokens can theoretically be reused until expiry. Tracked for P1 remediation.

---

## HT3 — Credential Isolation (HITRUST Control 09.j)
**CI: automated** | `secret-scan` job + `compliance-gates`

Credentials (API keys, HMAC secrets, OAuth secrets) MUST be isolated per environment.
The same `STEP_UP_SECRET` MUST NOT be used across tenants or across dev/prod boundaries.

**Isolation rules:**
1. `STEP_UP_SECRET` — per-deployment secret, auto-generated on Vercel, set manually on Railway
2. `MEDPLUM_CLIENT_SECRET` — per Medplum project, not shared across upstream configs
3. `SESSION_SECRET` — Flask session signing key, per deployment
4. `TELEGRAM_BOT_TOKEN` — per bot, not shared between environments

**Gate check:** CI uses `STEP_UP_SECRET=ci-test-secret` — must NOT match any production value.

---

## HT4 — Provenance and Data Integrity (HITRUST Control 09.aa)
**CI: automated** | `python-tests` includes Provenance creation on curatr_apply_fix

Every data quality fix applied via Curatr MUST create a linked `Provenance` resource
documenting: who changed it, what changed, when, and why (patient_intent).

**Provenance structure (created by `r6/curatr.py:apply_fix()`):**
```json
{
  "resourceType": "Provenance",
  "target": [{"reference": "Condition/{id}/_history/{version}"}],
  "recorded": "...",
  "activity": {"system": "http://terminology.hl7.org/CodeSystem/v3-DataOperation", "code": "UPDATE"},
  "agent": [{"type": {"code": "author"}, "who": {"display": "Patient via HealthClaw Curatr"}}],
  "reason": [{"code": "PATADMIN"}],
  "extension": [{
    "url": "https://healthclaw.io/fhir/StructureDefinition/curatr-correction",
    "valueObject": {
      "tool": "HealthClaw Curatr",
      "patient_intent": "(patient-supplied reason)",
      "changes_applied": 1,
      "change_summary": ["field_path updated"]
    }
  }]
}
```

**Gate check:** `curatr_apply_fix` response MUST include `provenance_id` field.
**Gate check:** Two `AuditEvent` records created per fix: one for the resource update, one for Provenance creation.

---

## HT5 — Access Control Policy (HITRUST Control 01.b) *(manual review)*
**Enforced via action_policy.yaml** | Reviewed during feature design

The `action_policy.yaml` file at repo root defines the allow/deny/approval matrix.
All new MCP tools or Flask routes that expose clinical data MUST have a corresponding
policy entry before deployment.

**Review checklist:**
- [ ] New tool listed in `action_policy.yaml` with appropriate `risk_level`
- [ ] `approval` mode set to `human_review` for any clinical write operation
- [ ] Policy entry reviewed and approved in PR by a second committer
- [ ] `rationale` field explains the access control decision

---

## HT6 — System Configuration Baseline (HITRUST Control 09.m) *(manual review)*
**Verified at deployment configuration time**

- [ ] Docker Compose `env_file: .env` — `.env` never committed (in `.gitignore`)
- [ ] `ALLOWED_ORIGINS` set to explicit domain list in production MCP server
- [ ] `FHIR_VALIDATOR_URL` either unset (silent fallback) or points to internal validator
- [ ] Redis `requirepass` set in production Redis configuration

---

## HT7 — Incident Response Readiness *(manual review)*
**Verified annually**

- [ ] `AuditEvent/$stream` endpoint operational — provides streaming audit trail for incident investigation
- [ ] `GET /AuditEvent?tenant_id={id}&_lastUpdated=ge{date}` works for post-incident queries
- [ ] Audit event retention policy documented (SQLite: limited by disk; PostgreSQL: separate retention policy needed)
- [ ] Runbook exists for: suspected PHI breach, token compromise, upstream FHIR server compromise
