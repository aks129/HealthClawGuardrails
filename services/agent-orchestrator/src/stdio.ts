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
import { executeMCPTool } from "./mcp-tool-result";

const { version: SERVER_VERSION } = require("../package.json") as {
  version: string;
};

const FHIR_BASE_URL =
  process.env.FHIR_BASE_URL || "http://localhost:5000/r6/fhir";

console.error(`[healthclaw-guardrails] stdio transport starting`);
console.error(`[healthclaw-guardrails] FHIR backend: ${FHIR_BASE_URL}`);

const fhirTools = new FHIRTools(FHIR_BASE_URL, { allowPrivileged: true });

const server = new Server(
  { name: "healthclaw-guardrails", version: SERVER_VERSION },
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

  return executeMCPTool(fhirTools, name, toolArgs, headers);
});

const transport = new StdioServerTransport();

server.connect(transport).then(() => {
  console.error(`[healthclaw-guardrails] connected via stdio`);
}).catch((err) => {
  console.error(`[healthclaw-guardrails] failed to connect:`, err);
  process.exit(1);
});
