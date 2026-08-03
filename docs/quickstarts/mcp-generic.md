# Quickstart: any MCP client (Claude Code, Cursor, LibreChat, custom agents)

**Endpoint (keyless demo):** `https://mcp-demo-production-ee2c.up.railway.app/mcp`
(Streamable HTTP; legacy SSE at `/sse` + `/messages`).

This is the **public demo server**. It needs no credentials and is hard-pinned
to the synthetic `desktop-demo` tenant, so it can only ever serve fake data.
Real tenants live on the production endpoint, which requires a bearer token —
see [Tenancy and auth](#tenancy-and-auth).

## Claude Code

```bash
claude mcp add --transport http healthclaw \
  https://mcp-demo-production-ee2c.up.railway.app/mcp
```

## Cursor / VS Code MCP config

```json
{
  "mcpServers": {
    "healthclaw": {
      "url": "https://mcp-demo-production-ee2c.up.railway.app/mcp"
    }
  }
}
```

## LibreChat (`librechat.yaml`)

```yaml
mcpServers:
  healthclaw:
    type: streamable-http
    url: https://mcp-demo-production-ee2c.up.railway.app/mcp
```

## No MCP client at all? Plain JSON-RPC bridge

```bash
curl -s -X POST \
  https://mcp-demo-production-ee2c.up.railway.app/mcp/rpc \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Tenancy and auth

There are two hosted endpoints, and which one you point at decides what the
tenancy headers below do.

| | Demo `mcp-demo-production-ee2c` | Production `mcp-server-production-5112` |
| --- | --- | --- |
| Credential | none | `Authorization: Bearer <token>` (401 without it) |
| Tenant | pinned to `desktop-demo` | selected by header/argument |
| Data | synthetic only | real tenants |

**On the demo endpoint, tenancy and bring-your-own-FHIR headers are ignored,
not honored.** The server drops them and answers from the pinned synthetic
tenant. That is deliberate — an open caller must not be able to reach a real
tenant — but it means a request that *looks* like it selected your tenant will
quietly return demo data instead. If you are passing any of the headers below,
you want the production endpoint.

On the production endpoint, **all of these are headers. None of them is a tool
argument.** Verified against the deployed server: no tool in `tools/list`
exposes `_tenantId`, `_stepUpToken`, `_fhirServerUrl` or `_fhirAccessToken` in
its input schema, and every schema is closed. Passing one as an argument is
silently dropped — the call still succeeds, against whatever tenant the
headers selected. This page previously claimed otherwise.

- `X-Tenant-Id` header selects a tenant.
- Non-public tenants need a tenant-bound token in `X-Step-Up-Token`. Writes
  always require step-up; clinical writes additionally require an explicit
  human confirmation (HTTP 428 otherwise).
- Bring-your-own FHIR server: `X-FHIR-Server-URL` (+ `X-FHIR-Access-Token`,
  `X-Patient-ID`) and the guardrail stack proxies it per-request
  (SHARP-on-MCP).

**A client that cannot set headers cannot select a tenant.** That is the whole
of #290: it is not that hosted connectors are inconvenient for real records,
it is that they have no mechanism to ask for one.

The production endpoint's bearer token is deployment-scoped and issued by the
operator; it is not self-serve. Note that hosted chat connectors (claude.ai,
ChatGPT, Perplexity) cannot attach a static bearer header, so they can only use
the demo endpoint today — tracked in
[#290](https://github.com/aks129/HealthClawGuardrails/issues/290).

## The tools

A hosted deployment serves **27**. The tool catalogue defines 29; two —
`fhir_get_token` and `fhir_seed` — are `PRIVILEGED_TOOL_NAMES` and are
withheld from network transports on purpose, so they will not appear in your
client's `tools/list`. That is deliberate, not a fault: minting step-up tokens
and seeding tenants are operator actions, not agent actions.

Read: `context_get`, `fhir_read`, `fhir_search`, `fhir_validate`,
`fhir_stats`, `fhir_lastn`, `fhir_permission_evaluate`,
`fhir_subscription_topics`, `questionnaire_populate`, `curatr_evaluate`,
`action_status`, `fhir_interpret_labs`, `care_gaps`, `guardrail_conformance`,
`wearables_sync_status`, `sources_check`, `fhir_compiled_truth`.
Write (step-up gated): `fhir_propose_write`, `fhir_commit_write`,
`curatr_apply_fix`, `action_propose`, `action_commit`, `rx_transfer_request`,
`shl_generate`, `questionnaire_extract`.
ChatGPT-connector shims: `search`, `fetch`.

Step-up for the write tier comes from the `X-Step-Up-Token` header, issued by
the operator or by the patient connect flow — not from a tool call, because
the tool that mints it is one of the two withheld above.

Run the [10-minute demo script](README.md#the-10-minute-demo-script-works-in-any-connected-agent)
from any of these clients.
