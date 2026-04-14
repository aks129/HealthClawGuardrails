# SOC2 Type II Compliance Gates

Gates for Security, Availability, and Confidentiality trust service criteria.
CI job `compliance-gates` validates S1–S4 automatically; S5–S8 require human review.

---

## S1 — Input Validation at Every Boundary
**CI: automated** | `python-tests` + `compliance-gates`

All incoming JSON payloads MUST be validated before processing. FHIR resources use
`$validate` (structural check) before any write is committed. Non-FHIR inputs use
schema checks at the Flask route layer.

**Enforcement:**
- FHIR writes: `r6/validator.py` called in all `POST`/`PUT` routes before DB write
- Bundle ingest: `POST /Bundle/$ingest-context` validates each entry before committing
- MCP tool args: TypeScript Zod-style checks in `tools.ts` handler `default:` branch

**Gate check:** `POST /Condition` with `{"resourceType": "invalid"}` must return 400 with `OperationOutcome`.

**Prohibited patterns:**
```python
# BAD — write without validate
db.session.add(R6Resource(...))

# GOOD
result = validator.validate_resource(body, ...)
if not result['valid']:
    return jsonify(make_outcome(...)), 400
```

---

## S2 — Structured Error Responses (No Stack Traces)
**CI: automated** | `compliance-gates` verifies OperationOutcome on error paths

All error responses MUST use FHIR `OperationOutcome` format. Python stack traces,
SQLAlchemy errors, and internal paths MUST NOT appear in HTTP responses.

**Enforcement:** Flask `@app.errorhandler` routes in `main.py`; route-level `try/except` blocks.
**Format:** `{"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "...", "diagnostics": "..."}]}`

**Gate check:** `GET /NonExistent/abc` must return `{"resourceType": "OperationOutcome"}`, not a traceback.

**Prohibited patterns:**
```python
# BAD — leaks internal path
except Exception as e:
    return str(e), 500

# GOOD
except Exception as e:
    logger.exception("Unexpected error in %s", resource_type)
    return jsonify(make_outcome('error', 'exception', 'An unexpected error occurred')), 500
```

---

## S3 — Dependency Vulnerability Audit
**CI: automated** | `dependency-audit` job runs `pip-audit` + `npm audit`

All Python and Node.js dependencies must pass vulnerability audit on every push.
High/critical CVEs block merge. Moderate CVEs require documented exception in PR.

**Commands:**
```bash
# Python
uv run pip-audit --requirement <(uv export --no-hashes)

# Node.js
cd services/agent-orchestrator && npm audit --audit-level=high
```

**Exception process:** Moderate CVEs that cannot be patched immediately require a
`security-exception:` comment in the PR body documenting: CVE ID, affected package,
why it's acceptable, and remediation timeline.

---

## S4 — Secrets Not in Source Code
**CI: automated** | `secret-scan` job runs `gitleaks`

No API keys, HMAC secrets, tokens, passwords, or credentials in committed files.
`gitleaks` scans every commit on push and PR, blocking merge on detection.

**Prohibited patterns:**
- `STEP_UP_SECRET = "hardcoded-value"` in any `.py`, `.ts`, `.yaml`, `.env` file
- `ANTHROPIC_API_KEY = "sk-ant-..."` 
- Any string matching `sk-ant-[a-zA-Z0-9-_]{80,}` pattern

**Allowed patterns:**
- `os.environ.get('STEP_UP_SECRET')` — environment variable lookup
- `process.env.ANTHROPIC_API_KEY` — environment variable lookup
- `${VARIABLE}` in docker-compose — compose variable substitution

**If a secret is accidentally committed:**
1. Rotate the secret immediately (do not wait)
2. Use `git filter-repo` to purge from history
3. Force-push after team notification
4. Document in security incident log

---

## S5 — Availability: Health Endpoint *(manual review)*
**Monitored in production** | `/r6/fhir/health` must return 200 with upstream status

The liveness probe at `/r6/fhir/health` MUST:
- Return HTTP 200 in local mode
- Report upstream FHIR server connectivity when `FHIR_UPSTREAM_URL` is set
- Return `{"status": "ok"}` or `{"status": "degraded", "upstream": false}` — never 500

**Review checklist:**
- [ ] `/r6/fhir/health` returns 200 after `docker compose up`
- [ ] Upstream probe sets `status: degraded` (not error) when upstream is unreachable
- [ ] Railway/Vercel health check configured to ping this endpoint

---

## S6 — Change Management: ETag Concurrency Control *(manual review)*
**Verified during code review**

All `PUT` (update) operations MUST support `If-Match` ETag concurrency control.
Stale-write conflicts MUST return 412 Precondition Failed, not silently overwrite.

**Enforcement:** `r6/routes.py` PUT handler checks `If-Match` vs current `version_id`.
**Review checklist:**
- [ ] New resource types supporting PUT include ETag check
- [ ] `version_id` incremented on every successful update (`update_resource()`)
- [ ] 412 returned when ETag doesn't match

---

## S7 — Rate Limiting *(manual review)*
**Verified at deployment configuration time**

The MCP server enforces `RATE_LIMIT_MAX` (default 120 req/min) per IP.
Flask uses Redis for rate limiting when `REDIS_URL` is set.

**Review checklist:**
- [ ] `RATE_LIMIT_MAX` environment variable set appropriately for production load
- [ ] Redis `REDIS_URL` configured (rate limiting degrades gracefully without Redis)
- [ ] MCP server CORS set to explicit `ALLOWED_ORIGINS` (not wildcard) in production

---

## S8 — Logging and Monitoring *(manual review)*
**Verified at deployment configuration time**

- `LOG_FORMAT=json` set in Railway/Vercel production environment
- Structured logs include: `event_type`, `resource_type`, `tenant_id`, `outcome` — never PHI values
- Audit log retention: AuditEventRecord rows must not be deleted (`APPEND-ONLY`)

**Review checklist:**
- [ ] `LOG_FORMAT=json` in production deployment
- [ ] No `LOG_LEVEL=DEBUG` in production (use `INFO` or `WARNING`)
- [ ] AuditEvent database table has no DELETE permissions for app user in production PostgreSQL
