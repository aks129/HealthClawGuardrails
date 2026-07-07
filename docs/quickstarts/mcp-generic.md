# Quickstart: any MCP client (Claude Code, Cursor, LibreChat, custom agents)

**Endpoint:** `https://mcp-server-production-5112.up.railway.app/mcp`
(Streamable HTTP; legacy SSE at `/sse` + `/messages`).

## Claude Code

```bash
claude mcp add --transport http healthclaw \
  https://mcp-server-production-5112.up.railway.app/mcp
```

## Cursor / VS Code MCP config

```json
{
  "mcpServers": {
    "healthclaw": {
      "url": "https://mcp-server-production-5112.up.railway.app/mcp"
    }
  }
}
```

## LibreChat (`librechat.yaml`)

```yaml
mcpServers:
  healthclaw:
    type: streamable-http
    url: https://mcp-server-production-5112.up.railway.app/mcp
```

## No MCP client at all? Plain JSON-RPC bridge

```bash
curl -s -X POST \
  https://mcp-server-production-5112.up.railway.app/mcp/rpc \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Tenancy and auth

- No headers → synthetic `desktop-demo` tenant (fake data; safe to demo).
- `X-Tenant-ID` header (or `_tenantId` tool argument) selects a tenant.
- Non-public tenants need a tenant-bound token: `X-Step-Up-Token` header or
  `_stepUpToken` argument. Writes always require step-up; clinical writes
  additionally require an explicit human confirmation (HTTP 428 otherwise).
- Bring-your-own FHIR server: pass `_fhirServerUrl` (+ `_fhirAccessToken`)
  and the guardrail stack proxies it per-request (SHARP-on-MCP).

## The 28 tools

Read: `context_get`, `fhir_read`, `fhir_search`, `fhir_validate`,
`fhir_stats`, `fhir_lastn`, `fhir_permission_evaluate`,
`fhir_subscription_topics`, `questionnaire_populate`, `curatr_evaluate`,
`action_status`, `fhir_interpret_labs`, `care_gaps`, `guardrail_conformance`.
Write (step-up gated): `fhir_propose_write`, `fhir_commit_write`,
`curatr_apply_fix`, `action_propose`, `action_commit`, `shl_generate`,
`questionnaire_extract`. Utility: `fhir_compiled_truth`, `fhir_get_token`,
`fhir_seed`, and friends.

Run the [10-minute demo script](README.md#the-10-minute-demo-script-works-in-any-connected-agent)
from any of these clients.
