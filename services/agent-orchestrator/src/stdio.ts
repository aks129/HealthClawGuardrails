/**
 * Stdio entry point for Claude Desktop / Claude Code MCP integration.
 *
 * Claude Desktop launches this process directly and communicates over
 * stdin/stdout using the MCP stdio transport. All log output MUST go
 * to stderr only — stdout is reserved for JSON-RPC messages.
 *
 * Usage (claude_desktop_config.json):
 *   "command": "node",
 *   "args": ["dist/stdio.js"]
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { FHIRTools } from "./tools";

const FHIR_BASE_URL =
  process.env.FHIR_BASE_URL || "http://localhost:5000/r6/fhir";

console.error(`[healthclaw-guardrails] stdio transport starting`);
console.error(`[healthclaw-guardrails] FHIR backend: ${FHIR_BASE_URL}`);

const fhirTools = new FHIRTools(FHIR_BASE_URL);

const server = new Server(
  { name: "healthclaw-guardrails", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => {
  return { tools: fhirTools.getMCPToolSchemas() };
});

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  const toolArgs = (args ?? {}) as Record<string, unknown>;

  // Extract internal headers injected via tool args
  const headers: Record<string, string> = {};
  if (typeof toolArgs._tenantId === "string") {
    headers["x-tenant-id"] = toolArgs._tenantId;
    delete toolArgs._tenantId;
  }
  if (typeof toolArgs._stepUpToken === "string") {
    headers["x-step-up-token"] = toolArgs._stepUpToken;
    delete toolArgs._stepUpToken;
  }
  if (typeof toolArgs._authorization === "string") {
    headers["authorization"] = toolArgs._authorization;
    delete toolArgs._authorization;
  }
  if (typeof toolArgs._humanConfirmed === "string") {
    headers["x-human-confirmed"] = toolArgs._humanConfirmed;
    delete toolArgs._humanConfirmed;
  }

  try {
    const result = await fhirTools.executeTool(name, toolArgs, headers);
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`[healthclaw-guardrails] tool error (${name}):`, msg);
    return {
      content: [{ type: "text", text: JSON.stringify({ error: msg }) }],
      isError: true,
    };
  }
});

const transport = new StdioServerTransport();

server.connect(transport).then(() => {
  console.error(`[healthclaw-guardrails] connected via stdio`);
}).catch((err) => {
  console.error(`[healthclaw-guardrails] failed to connect:`, err);
  process.exit(1);
});
