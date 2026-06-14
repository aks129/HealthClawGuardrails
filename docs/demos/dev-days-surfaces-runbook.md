# Dev Days — Three Live Surfaces Runbook

Companion to `dev-days-presentation.md`. Three things to show live:

1. **Sally surveys all connected services at once** (Telegram)
2. **Command Center** — live telemetry dashboard
3. **FHIR Control Panel** — server visualization: inventory, explorer, profile adherence

All three are deployed. Demo tenant: **`ev-personal`** (real MEDENT record, ~274 resources) via a short-lived signed link; `desktop-demo` (synthetic) is the open fallback.

---

## Pre-flight (15 min before)

```bash
# 1. Services healthy
curl -s https://app.healthclaw.io/r6/fhir/health | jq .status        # "ok"
curl -s https://mcp-server-production-5112.up.railway.app/health      # {"status":"ok"}

# 2. MCP has the demo tools (expect 21, including sources_check + shl_generate)
curl -s -X POST https://mcp-server-production-5112.up.railway.app/mcp/rpc \
  -H "content-type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | python3 -c "import json,sys; t=json.load(sys.stdin)['result']['tools']; print(len(t),'tools'); print('sources_check' in [x['name'] for x in t])"

# 3. ev-personal has data (control panel + command center need this)
curl -s -H "X-Tenant-Id: ev-personal" "https://app.healthclaw.io/r6/fhir/\$inventory" \
  | python3 -c "import json,sys; p=json.load(sys.stdin)['parameter']; print([x for x in p if x['name']=='total'])"
```

### Generate the command-center signed link (24h TTL — do this the morning of)

```bash
TOK=$(curl -s -X POST https://app.healthclaw.io/r6/fhir/internal/step-up-token \
  -H "X-Tenant-Id: ev-personal" -H "content-type: application/json" \
  -d '{"tenant_id":"ev-personal"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['token'])")
curl -s -X POST https://app.healthclaw.io/command-center/api/generate-link \
  -H "content-type: application/json" -H "X-Tenant-Id: ev-personal" -H "X-Step-Up-Token: $TOK" \
  -d '{"tenant_id":"ev-personal"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['url'])"
```
Open that URL in a pre-loaded browser tab. (The FHIR Control Panel page shell is public — no link needed — but its data calls carry `?tenant=ev-personal`.)

### Pre-open tabs
| Tab | URL |
|---|---|
| Telegram | Sally — PCP Advisor |
| Command Center | the signed-link URL above |
| FHIR Control Panel | `https://app.healthclaw.io/fhir-control-panel?tenant=ev-personal` |
| CapabilityStatement | `https://app.healthclaw.io/r6/fhir/metadata` (one click from the panel) |

---

## Act 1 — Sally checks all services at once (3 min)

In Telegram, ask Sally:

> **"Check all my connected services for data."**

She runs `fhir_get_token` → `sources_check` (one MCP call to `/command-center/api/sources-summary`) and replies with a single summary:

- **Connected: N / 7** — Fasten ✓, HealthEx, Health Bank One ✓, MEDENT ✓, Flexpa, Epic/Health Skillz, Open Wearables
- **Total records: ~274** — by type (57 Conditions, … Observations, MedicationRequests, …)

**Say:** "One question, one guarded MCP call — she surveys every data pipe at once. Connection status and counts only; no clinical values cross into a chat summary. The same tool works from the web app and Claude Desktop."

*Fallback:* if Sally defers (deferred tools not loaded), prompt "use the sources_check tool." If the gateway is cold, `desktop-demo` answers without a step-up token.

---

## Act 2 — Command Center telemetry (4 min)

Open the signed-link tab. It polls every 5 seconds against live SQLite — no cache.

Walk the panels top to bottom:
- **Hero stats** — records, flags, pending tasks, actions/24h (live counts)
- **Readiness pipeline** — Stack Live → Data Connected → Records Ingested → Quality Curated → Insights Running
- **System status** — Flask, MCP server, OpenClaw gateway, Redis (all green now that `MCP_HEALTH_URL` points at the hosted MCP)
- **Agents / Latest Actions / Conversations** — the audit stream updating as Act 1's calls land
- **Data Sources** + **Skills**

**Say:** "Every number is a live query, refreshed every five seconds. The Latest Actions feed is the append-only audit trail — watch the `sources_check` and `fhir_get_token` calls from a moment ago show up with agent attribution. This is the same guardrail audit that makes agent access defensible."

**Tip:** trigger Act 1 *during* Act 2 setup so the audit feed visibly updates on stage.

---

## Act 3 — FHIR Control Panel (4 min)

Open `…/fhir-control-panel?tenant=ev-personal`.

- **Resource Inventory** — tile per type with live counts ("274 resources across N types"). Click a tile.
- **Profile Adherence** — overall % gauge (green ≥90) + per-type table: conformant/sampled, adherence %, top missing US Core fields. **Say:** "This is conformance you can see — every resource run through the US Core v9 required-field validator, rolled up. Where a field is missing, it's named."
- **Resource Explorer** — the selected type's most-recent resources (PHI-redacted), each row → the compiled-truth detail page (current state + append-only Provenance timeline).
- **CapabilityStatement** link — the FHIR `/metadata` declaring R4 + R6 ballot resources and guardrail operations.

**Say:** "A FHIR server you can actually see — what's in it, how conformant it is, and the full evidence trail behind any single resource. All reads here are redacted and audited, same as everything else."

---

## Fallbacks

| Problem | Recovery |
|---|---|
| Signed link expired | Re-run the generate-link block (24h TTL) |
| Command center shows "auth required" | Link's `t=` token expired or wrong tenant — regenerate |
| Sally won't call the tool | "use the sources_check tool"; or demo on `desktop-demo` (no token) |
| MCP "down" in system status | `MCP_HEALTH_URL` not picked up — confirm it's set on the Flask service and redeploy |
| Control panel empty | Wrong/empty tenant — confirm `?tenant=ev-personal`; `$inventory` total should be >0 |
| Real PHI on a public projector | Switch any tab's tenant to `desktop-demo` (synthetic) |

## URLs (quick copy)

- Control Panel: `https://app.healthclaw.io/fhir-control-panel?tenant=ev-personal`
- Command Center: signed link (generate fresh)
- CapabilityStatement: `https://app.healthclaw.io/r6/fhir/metadata`
- Sources summary (raw): `GET /command-center/api/sources-summary?tenant=<id>` (token-gated)
- Inventory / adherence (raw): `GET /r6/fhir/$inventory`, `GET /r6/fhir/$profile-adherence` (tenant header)
