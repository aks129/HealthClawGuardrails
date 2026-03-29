# Skill Standards — Facts and Patterns

Last updated: 2026-03-29

## Frontmatter Fields Reference

| Field | Required | Type | Notes |
|---|---|---|---|
| `name` | Yes | string | kebab-case, must match folder name |
| `description` | Yes | string | Trigger language; may use YAML `>` block scalar |
| `metadata` | No | **single-line JSON** | NOT multi-line YAML |
| `disable-model-invocation` | No | boolean (default false) | Set true for runtime/infra skills |
| `user-invocable` | No | boolean (default true) | False hides from slash-command list |
| `homepage` | No | URL | Shown as "Website" in macOS Skills UI |
| `command-dispatch` | No | `"tool"` | Bypasses model, routes directly to a tool |
| `command-tool` | No | string | Tool name when `command-dispatch: tool` |
| `license` | No | string | Used by Anthropic reference skills |

## Metadata Schema (OpenClaw)

```json
{
  "openclaw": {
    "requires": {
      "env": ["ENV_VAR_NAME"],
      "bins": ["node", "python3"],
      "anyBins": ["..."],
      "config": ["browser.enabled"]
    },
    "install": [
      {"kind": "node", "packages": ["pkg1", "pkg2"]},
      {"kind": "uv", "packages": ["pkg1"]}
    ],
    "primaryEnv": "ENV_VAR_NAME",
    "always": false,
    "emoji": "🔒",
    "os": ["darwin", "linux"],
    "skillKey": "override-key"
  }
}
```

## Skill Load Precedence (highest to lowest)

1. Workspace skills (`/path/to/workspace/.claude/skills/`)
2. Project agent skills
3. Personal agent skills
4. Managed/local skills
5. Bundled skills
6. Extra directories

## Anthropic Reference Skill Patterns (observed)

- **pdf**: `name` + `description` + `license` only. No metadata. Body is implementation guide.
- **mcp-builder**: `name` + `description` + `license`. Body is phased development guide.
- **claude-api**: `name` + `description`. Body is language-specific API usage guide.
- **webapp-testing**: `name` + `description`. Body is Playwright patterns.

Pattern: Anthropic skills are minimal in frontmatter and dense in body content.

## HealthClaw Skills Audit (2026-03-29)

| Skill | Compliance Issues Found | Fixed |
|---|---|---|
| `curatr` | `metadata` was multi-line YAML — non-compliant | Yes — converted to single-line JSON |
| `fhir-r6-guardrails` | None | N/A |
| `phi-redaction` | None | N/A |
| `fhir-upstream-proxy` | Stale version in body (`0.9.0`) | Yes — updated to `1.0.0` |

Also updated `r6/fhir_proxy.py` User-Agent from `MCP-FHIR-Guardrails/0.9.0` to `HealthClaw-Guardrails/1.0.0`.

---

## Fasten Connect Integration Research (2026-03-29)

### What Fasten Connect Is

Patient-mediated FHIR data retrieval platform. Patients authorize access to EHR data
(Epic, Cerner, Athena, etc.) via Fasten Stitch (web widget or React Native SDK).
Backend API initiates bulk EHI export; data arrives as FHIR R4 NDJSON files via webhook.

### Key API Facts

| Item | Detail |
|---|---|
| Auth | HTTP Basic — `public_*` + `private_*` API keys |
| Initiate export | `POST /v1/bridge/fhir/ehi-export` with `org_connection_id` |
| Poll status | `GET /v1/bridge/fhir/ehi-export/{org_connection_id}` |
| Download | `GET /v1/bridge/fhir/ehi-export/{task_id}/download/{filename}` |
| File format | FHIR R4 JSONL / NDJSON |
| File size | 30MB – 3GB+ |
| Download TTL | 24 hours (links expire in ~10 min) |
| Idempotent | Yes — duplicate `org_connection_id` returns existing job |

### TEFCA IAS Key Facts

- Enable with `tefca-mode="true"` on Stitch widget
- Identity verification via CLEAR (phone+email) or ID.me (username+password)
- No per-provider logins — single identity verification retrieves across all QHINs
- Scope always returns `patient/*.read`
- May fail with `tefca_no_documents_found` when health systems return no records
- Use `tefca_directory_id` as stable identifier (not `endpoint_id`/`portal_id`/`brand_id`)
- Additional fees in live mode

### Webhook Events

| Event | Trigger |
|---|---|
| `patient.ehi_export_success` | Export complete — includes `download_links` array |
| `patient.ehi_export_failed` | Export failed — includes `failure_reason` enum |
| `patient.connection_success` | Patient authorized (disabled by default) |
| `patient.authorization_revoked` | Consent expired or revoked |
| `patient.request_health_system` | Patient requests unsupported health system |
| `webhook.test` | Webhook config test |

### Integration Fit with HealthClaw Guardrails

Fasten Connect is the **ingestion layer** upstream of HealthClaw Guardrails:

```text
Patient ──[Fasten Stitch Widget]──> Fasten Connect
                                           │
                                    webhook fires
                                           │
                              Flask webhook endpoint (new)
                               /fasten/webhook
                                           │
                              Download NDJSON (streaming)
                                           │
                              Ingest FHIR resources into DB
                                           │
                              MCP tools expose with guardrails
```

### Skill Feasibility: HIGH

A `fasten-connect` skill is feasible and architecturally natural. It solves the
"where does the data come from" gap in HealthClaw (currently only local JSON or
upstream proxy — no patient-authorized ingestion path).

**Required new code (not in skill file itself):**

- Flask Blueprint: `/fasten/webhook` (receives Fasten webhook events)
- NDJSON streaming downloader (handles 30MB–3GB files)
- Resource ingester (parses FHIR R4 NDJSON → existing FHIR resource DB)
- Stitch widget integration in `r6_dashboard.html` or a new `/connect` page

**Skill file scope:** Developer reference — documents the integration pattern,
env vars, webhook setup, data flow, and agent usage. `disable-model-invocation: true`.
