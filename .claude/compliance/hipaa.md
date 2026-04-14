# HIPAA Compliance Gates

Enforced gates for any code change touching PHI access, audit trails, redaction,
or access controls. Each gate must pass before a feature is considered complete.
CI job `compliance-gates` validates H1–H4 automatically; H5–H8 require human review.

---

## H1 — Audit Trail on Every Access
**CI: automated** | `compliance-gates` job verifies AuditEvent written on read + write

Every FHIR resource access (read, search, create, update, delete) MUST emit an
`AuditEventRecord` in the same request transaction. Failure to record is treated
as a security incident.

**Enforcement:** `r6/routes.py` — every route calls `_emit_audit()` before returning.
**Model:** `r6/models.py:AuditEventRecord` — append-only; `@db.event` blocks UPDATE/DELETE.
**Gate check:** `GET /AuditEvent?_count=1` after a read must return ≥ 1 record with matching `resource_type` and `tenant_id`.

**Prohibited patterns:**
```python
# BAD — returns without audit
return jsonify(resource), 200

# GOOD
_emit_audit(event_type='read', resource_type=rt, resource_id=rid, tenant_id=tid)
return jsonify(resource), 200
```

---

## H2 — PHI Redaction on All External Read Paths
**CI: automated** | `compliance-gates` verifies `family` name is truncated to initials

PHI fields MUST be redacted before any response leaves the guardrail boundary.
This applies to: REST reads, MCP tool responses, upstream proxy responses, Telegram bot output.

**Redaction profiles:**
- `apply_redaction(resource)` — standard HIPAA Safe Harbor (18 identifiers stripped)
- `apply_patient_controlled_redaction(resource, patient_id)` — preserves birthDate, strips institutional IDs

**Fields stripped (Safe Harbor):** name.family/given (→ initials), address, telecom, SSN/MRN/identifiers (→ `REDACTED-XXXX`), photo, birthDate (→ year only), narrative text.

**Gate check:** Response `name[0].family` must match `^[A-Z]\.$` pattern (single initial).

**Prohibited patterns:**
```python
# BAD — raw resource returned without redaction
return jsonify(resource.to_fhir_json()), 200

# GOOD
from r6.redaction import apply_redaction
redacted = apply_redaction(resource.to_fhir_json())
return jsonify(redacted), 200
```

---

## H3 — Minimum Necessary Access (Tenant Isolation)
**CI: automated** | `compliance-gates` verifies cross-tenant reads return 404

Every database query MUST filter by `tenant_id`. Cross-tenant data access is a
HIPAA breach. Tenant is sourced from `X-Tenant-ID` header — never from request body.

**Enforcement:** All `R6Resource.query.filter_by(...)` calls include `tenant_id=tenant_id`.
**Gate check:** Seeding tenant A then reading as tenant B MUST return 404 (not the resource).

**Prohibited patterns:**
```python
# BAD — no tenant filter
resource = R6Resource.query.get(resource_id)

# GOOD
resource = R6Resource.query.filter_by(id=resource_id, tenant_id=tenant_id).first()
```

---

## H4 — Write Authorization (Step-Up + Human-in-the-Loop)
**CI: automated** | `compliance-gates` verifies 401 on missing token, 428 on missing confirmation

All clinical write operations require:
1. `X-Step-Up-Token` header (HMAC-SHA256, 5-min TTL) — enforced before any DB write
2. `X-Human-Confirmed: true` header — for `Observation`, `Condition`, `MedicationRequest`, `Procedure`, `DiagnosticReport`, `CarePlan`, `Consent`

**Enforcement:** `r6/stepup.py:validate_step_up_token()` + `r6/health_compliance.py:enforce_human_in_loop()`
**HTTP codes:** Missing token → 401. Missing human confirmation → 428 Precondition Required.

**Gate check:** `POST /Patient` without `X-Step-Up-Token` must return 401.
**Gate check:** `POST /Condition` with valid token but no `X-Human-Confirmed` must return 428.

---

## H5 — PHI Not in Logs *(manual review)*
**CI: not automatable** | Reviewed during PR approval

Log statements MUST NOT include: `name`, `address`, `birthDate`, `identifier`, `telecom`
or any raw FHIR resource JSON that has not been redacted.

**Prohibited patterns:**
```python
# BAD
logger.info(f"Fetched patient: {resource}")
logger.debug(f"name={patient['name']}")

# GOOD
logger.info(f"Fetched resource type={resource_type} id={resource_id} tenant={tenant_id}")
```

**Review checklist:**
- [ ] No `str(resource)` or `json.dumps(resource)` in log statements
- [ ] No `f"...{patient['name']}..."` patterns
- [ ] Log format contains only IDs, types, and tenant — never clinical values

---

## H6 — Encryption in Transit *(manual review)*
**CI: not automatable** | Verified at deployment configuration time

- All upstream FHIR server connections use HTTPS (`FHIR_UPSTREAM_URL` must start with `https://` in production)
- Flask sessions use signed cookies (`SESSION_SECRET` must be set and not the dev default)
- Redis connections use TLS in production (`REDIS_URL` starts with `rediss://`)

**Review checklist:**
- [ ] `FHIR_UPSTREAM_URL` uses `https://` in Railway/Vercel environment vars
- [ ] `SESSION_SECRET` overridden from dev default in all production deployments
- [ ] `STEP_UP_SECRET` is random 32+ byte value, not the dev default

---

## H7 — De-identification Before Export *(manual review)*
**CI: not automatable** | Verified during export script usage

`scripts/export_healthex.py` applies patient-controlled de-identification by default.
Passing `--no-deidentify` requires explicit justification documented in commit message.

**Review checklist:**
- [ ] `--no-deidentify` flag not used without documented justification
- [ ] Exported bundles in `exports/` are gitignored (`.gitignore` includes `exports/`)
- [ ] Bundle files not committed to the repository

---

## H8 — Business Associate Agreement Reference *(manual review)*
**Reviewed before production deployment**

- [ ] Upstream FHIR server operator has a signed BAA on file
- [ ] Railway/Vercel BAAs in place for any environment storing real PHI
- [ ] This repository does not store real PHI — demo data only (`urn:healthclaw:patient` canonical IDs)
