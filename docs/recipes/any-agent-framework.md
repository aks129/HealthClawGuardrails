# Recipe: run HealthClaw tools on any agent framework

**Goal:** use the 23 guardrailed HealthClaw tools from an agent built on
**OpenAI**, **Google Gemini**, **LangChain**, or plain HTTP — not just Claude/MCP.
The guardrails (redaction, audit, step-up, tenant isolation) are enforced
**server-side**, so every framework gets the same safety; the client side is just
a tool-schema shim.

## The three doors

| Framework | Easiest path | Effort |
| --- | --- | --- |
| **Claude Desktop / Code, Hermes** | Native MCP (`/mcp` streamable HTTP or stdio) | none — already works |
| **OpenAI (Agents SDK / Responses API)** | (a) point its **remote-MCP connector** at `POST /mcp`, or (b) `adapters` bridge below | XS |
| **Gemini (Vertex / Gemini API)** | `adapters` bridge (no native remote-MCP connector) | S |
| **LangChain / LlamaIndex** | community MCP adapters, or the bridge | XS |
| **Anything (any language)** | `POST /mcp/rpc` JSON-RPC bridge directly | XS |

## The bridge (`adapters/`)

- `adapters/tools.manifest.json` — the 23 tools as JSON Schema (regenerate any time
  from the MCP server: `POST /mcp/rpc {"method":"tools/list"}`).
- `adapters/healthclaw_bridge.py`:
  - `to_openai_tools(manifest)` → OpenAI function tools
  - `to_gemini_declarations(manifest)` → Gemini FunctionDeclarations (schema reduced to Gemini's subset)
  - `HealthClawClient(mcp_base_url, tenant_id, step_up_token).call(name, args)` →
    relays to `/mcp/rpc`, carrying `X-Tenant-Id` / `X-Step-Up-Token`.

Read-tier tools need only `X-Tenant-Id` (public tenants) or a tenant-bound token;
write-tier tools (`fhir_commit_write`, `action_commit`, `shl_generate`, `questionnaire_extract`)
require `X-Step-Up-Token`. Mint one with `fhir_get_token` or
`POST /r6/fhir/internal/step-up-token`.

## OpenAI (Agents SDK / Chat Completions)

Two options:

1. **Remote MCP** — point the OpenAI remote-MCP connector at `https://<mcp-host>/mcp`
   and forward `X-Tenant-Id` / `X-Step-Up-Token` as custom headers. No code from this repo.
2. **Bridge** — `adapters/examples/openai_agent.py`: builds `tools=to_openai_tools(...)`,
   runs the tool-calling loop, and dispatches each `tool_call` through `HealthClawClient`.

## Gemini

Gemini has no turnkey remote-MCP connector, so use the bridge:
`adapters/examples/gemini_agent.py` maps `to_gemini_declarations(...)` into a
`Tool(function_declarations=...)`, and relays each `functionCall` to `/mcp/rpc`,
returning the result as a `functionResponse`.

## Skills

A `skills/*/SKILL.md` is a system-prompt fragment + a set of tool calls. To port
a skill to OpenAI/Gemini: inline the `SKILL.md` body as the system/developer prompt
and let the tool mechanics ride the manifest. No new machine format needed.

## Why this matters

Partners run different agent stacks. "HealthClaw guards **any** FHIR server
([Medplum recipe](healthclaw-in-front-of-medplum.md)) **under any** agent
framework" is the full portability story — and the guardrails never move client-side,
so no framework can bypass them.
