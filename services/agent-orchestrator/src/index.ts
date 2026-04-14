/**
 * FHIR R6 Agent Orchestrator - MCP Server
 *
 * Uses the official @modelcontextprotocol/sdk to expose FHIR tools
 * via the Model Context Protocol.
 *
 * Transports (priority order):
 * 1. Streamable HTTP: POST /mcp (preferred — OpenAI & Anthropic compatible)
 * 2. SSE: GET /sse + POST /messages (legacy MCP transport)
 * 3. HTTP bridge: POST /mcp/rpc (convenience for non-MCP Python clients)
 *
 * Security:
 * - CORS with deny-by-default (requires explicit ALLOWED_ORIGINS)
 * - Origin header validation (DNS rebinding protection)
 * - Rate limiting per-client
 * - OAuth bearer token forwarding
 * - Tenant + step-up header forwarding
 */

import express from "express";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { SSEServerTransport } from "@modelcontextprotocol/sdk/server/sse.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import crypto from "crypto";
import { FHIRTools } from "./tools";

const app = express();
app.use(express.json());

const PORT = process.env.MCP_PORT || 3001;
const FHIR_BASE_URL =
  process.env.FHIR_BASE_URL || "http://localhost:5000/r6/fhir";
const ALLOWED_ORIGINS = (process.env.ALLOWED_ORIGINS || "").split(",").filter(Boolean);

// Initialize FHIR tools
const fhirTools = new FHIRTools(FHIR_BASE_URL);

// Supported MCP protocol versions (newest first)
const SUPPORTED_PROTOCOL_VERSIONS = ["2024-11-05"];

// --- CORS Middleware (deny-by-default) ---

app.use((req, res, next) => {
  const origin = req.headers.origin;
  if (origin && ALLOWED_ORIGINS.length > 0 && ALLOWED_ORIGINS.includes(origin)) {
    res.setHeader("Access-Control-Allow-Origin", origin);
  }
  // If ALLOWED_ORIGINS is empty, no Access-Control-Allow-Origin is set (deny-by-default)
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
  res.setHeader(
    "Access-Control-Allow-Headers",
    "Content-Type, Authorization, X-Tenant-Id, X-Step-Up-Token, X-Agent-Id, X-Human-Confirmed, Mcp-Session-Id"
  );
  res.setHeader("Access-Control-Expose-Headers", "Mcp-Session-Id");
  if (req.method === "OPTIONS") {
    return res.sendStatus(204);
  }
  next();
});

// --- Rate Limiting (in-memory, per IP) ---

const rateLimitMap = new Map<string, { count: number; resetAt: number }>();
const RATE_LIMIT_WINDOW_MS = 60_000;
const RATE_LIMIT_MAX = parseInt(process.env.RATE_LIMIT_MAX || "120", 10);

function checkRateLimit(ip: string): boolean {
  const now = Date.now();
  const entry = rateLimitMap.get(ip);
  if (!entry || now > entry.resetAt) {
    rateLimitMap.set(ip, { count: 1, resetAt: now + RATE_LIMIT_WINDOW_MS });
    return true;
  }
  entry.count++;
  return entry.count <= RATE_LIMIT_MAX;
}

app.use((req, res, next) => {
  const clientIp = req.ip || req.socket.remoteAddress || "unknown";
  if (!checkRateLimit(clientIp)) {
    return res.status(429).json({
      jsonrpc: "2.0",
      error: { code: -32000, message: "Rate limit exceeded" },
    });
  }
  next();
});

// --- Helper: extract forwarded headers from HTTP request ---

function extractHeaders(req: express.Request): Record<string, string> {
  const h: Record<string, string> = {};
  const tenantId = req.headers["x-tenant-id"];
  if (typeof tenantId === "string") h["x-tenant-id"] = tenantId;
  const stepUp = req.headers["x-step-up-token"];
  if (typeof stepUp === "string") h["x-step-up-token"] = stepUp;
  const agentId = req.headers["x-agent-id"];
  if (typeof agentId === "string") h["x-agent-id"] = agentId;
  const auth = req.headers["authorization"];
  if (typeof auth === "string") h["authorization"] = auth;
  const humanConfirmed = req.headers["x-human-confirmed"];
  if (typeof humanConfirmed === "string") h["x-human-confirmed"] = humanConfirmed;
  return h;
}

// --- MCP Server Factory (creates per-session server instances) ---
//
// sessionHeaders: HTTP request headers captured when the session was initiated.
// For SSE these come from the GET /sse connection; for Streamable HTTP the
// tools/call handler re-extracts headers per-request and bypasses this factory,
// so sessionHeaders is only meaningfully used on the SSE path.

function createMCPServer(sessionHeaders: Record<string, string> = {}): Server {
  const server = new Server(
    { name: "healthclaw-guardrails", version: "1.0.0" },
    { capabilities: { tools: {}, logging: {} } }
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => {
    return { tools: fhirTools.getMCPToolSchemas() };
  });

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    const toolArgs = (args ?? {}) as Record<string, unknown>;

    // Start with session-level headers (captured at connection time for SSE).
    // Tool-arg headers (_tenantId, _stepUpToken, _authorization) override session
    // headers, allowing per-call overrides without changing the connection.
    const toolHeaders: Record<string, string> = { ...sessionHeaders };
    if (typeof toolArgs._tenantId === "string") {
      toolHeaders["x-tenant-id"] = toolArgs._tenantId as string;
      delete toolArgs._tenantId;
    }
    if (typeof toolArgs._stepUpToken === "string") {
      toolHeaders["x-step-up-token"] = toolArgs._stepUpToken as string;
      delete toolArgs._stepUpToken;
    }
    if (typeof toolArgs._authorization === "string") {
      toolHeaders["authorization"] = toolArgs._authorization as string;
      delete toolArgs._authorization;
    }

    const result = await fhirTools.executeTool(name, toolArgs, toolHeaders);
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  });

  return server;
}

// --- Streamable HTTP Transport (preferred — /mcp endpoint) ---

const streamableSessions = new Map<string, Server>();

// Negotiate protocol version: pick the best match between client and server
function negotiateProtocolVersion(clientVersion?: string): string {
  if (clientVersion && SUPPORTED_PROTOCOL_VERSIONS.includes(clientVersion)) {
    return clientVersion;
  }
  return SUPPORTED_PROTOCOL_VERSIONS[0]; // Default to latest supported
}

app.post("/mcp", async (req, res) => {
  // Origin validation (DNS rebinding protection)
  const origin = req.headers.origin;
  if (origin && ALLOWED_ORIGINS.length > 0 && !ALLOWED_ORIGINS.includes(origin)) {
    return res.status(403).json({
      jsonrpc: "2.0",
      error: { code: -32600, message: "Origin not allowed" },
    });
  }

  const body = req.body;
  if (!body || !body.jsonrpc) {
    return res.status(400).json({
      jsonrpc: "2.0",
      error: { code: -32600, message: "Invalid JSON-RPC request" },
    });
  }

  const reqHeaders = extractHeaders(req);
  const { id, method, params } = body;

  try {
    switch (method) {
      case "initialize": {
        // Server ALWAYS generates session ID (prevent session fixation)
        const sessionId = crypto.randomUUID();
        const server = createMCPServer();
        streamableSessions.set(sessionId, server);

        // Protocol version negotiation
        const clientVersion = params?.protocolVersion as string | undefined;
        const negotiatedVersion = negotiateProtocolVersion(clientVersion);

        res.setHeader("Mcp-Session-Id", sessionId);
        return res.json({
          jsonrpc: "2.0",
          id,
          result: {
            protocolVersion: negotiatedVersion,
            capabilities: { tools: {}, logging: {} },
            serverInfo: { name: "healthclaw-guardrails", version: "1.0.0" },
          },
        });
      }

      case "notifications/initialized": {
        // Notifications have no id and no response per JSON-RPC spec
        return res.sendStatus(204);
      }

      case "tools/list": {
        const tools = fhirTools.getMCPToolSchemas();
        return res.json({ jsonrpc: "2.0", id, result: { tools } });
      }

      case "tools/call": {
        // Require valid session for tool calls
        const sessionId = req.headers["mcp-session-id"] as string;
        if (!sessionId || !streamableSessions.has(sessionId)) {
          return res.status(400).json({
            jsonrpc: "2.0",
            id,
            error: { code: -32600, message: "Invalid or missing session. Call initialize first." },
          });
        }

        const toolName = params?.name as string;
        const toolInput = (params?.arguments ?? {}) as Record<string, unknown>;

        if (!toolName) {
          return res.json({
            jsonrpc: "2.0",
            id,
            error: { code: -32602, message: "Missing tool name" },
          });
        }

        const result = await fhirTools.executeTool(toolName, toolInput, reqHeaders);
        return res.json({
          jsonrpc: "2.0",
          id,
          result: {
            content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
          },
        });
      }

      default:
        return res.json({
          jsonrpc: "2.0",
          id,
          error: { code: -32601, message: `Method not found: ${method}` },
        });
    }
  } catch (error: unknown) {
    const detail = error instanceof Error ? error.message : "Unknown error";
    console.error(`Streamable HTTP error for ${method}:`, detail);
    return res.json({
      jsonrpc: "2.0",
      id,
      error: { code: -32603, message: "Internal error" },
    });
  }
});

// DELETE /mcp — session cleanup
app.delete("/mcp", (req, res) => {
  const sessionId = req.headers["mcp-session-id"] as string;
  if (sessionId) {
    streamableSessions.delete(sessionId);
  }
  res.sendStatus(204);
});

// --- Session cleanup: expire sessions after 30 minutes of inactivity ---
setInterval(() => {
  // In production, sessions would have last-activity timestamps
  // For now, cap total sessions to prevent memory exhaustion
  const MAX_SESSIONS = 1000;
  if (streamableSessions.size > MAX_SESSIONS) {
    const iterator = streamableSessions.keys();
    const toDelete = streamableSessions.size - MAX_SESSIONS;
    for (let i = 0; i < toDelete; i++) {
      const key = iterator.next().value;
      if (key) streamableSessions.delete(key);
    }
  }
}, 60_000);

// --- SSE Transport (legacy MCP, still supported) ---

const activeSessions = new Map<string, { transport: SSEServerTransport; headers: Record<string, string> }>();

app.get("/sse", async (req, res) => {
  // Capture headers from the SSE connection request and pass them into the
  // server instance so CallToolRequestSchema forwwards X-Tenant-ID on every tool call.
  const reqHeaders = extractHeaders(req);
  const server = createMCPServer(reqHeaders);
  const transport = new SSEServerTransport("/messages", res);
  activeSessions.set(transport.sessionId, { transport, headers: reqHeaders });

  res.on("close", () => {
    activeSessions.delete(transport.sessionId);
  });

  await server.connect(transport);
});

app.post("/messages", async (req, res) => {
  const sessionId = req.query.sessionId as string;
  const session = activeSessions.get(sessionId);
  if (!session) {
    return res.status(400).json({ error: "Invalid or expired session" });
  }
  await session.transport.handlePostMessage(req, res);
});

// --- Legacy HTTP Bridge (for Python agent_client) ---

interface JSONRPCRequest {
  jsonrpc: string;
  id: string | number;
  method: string;
  params?: Record<string, unknown>;
}

app.post("/mcp/rpc", async (req, res) => {
  const rpcRequest: JSONRPCRequest = req.body;

  if (!rpcRequest || rpcRequest.jsonrpc !== "2.0" || !rpcRequest.method) {
    return res.status(400).json({
      jsonrpc: "2.0",
      error: { code: -32600, message: "Invalid JSON-RPC request" },
      id: rpcRequest?.id ?? null,
    });
  }

  const { id, method, params } = rpcRequest;
  const reqHeaders = extractHeaders(req);

  try {
    switch (method) {
      case "tools/list": {
        const tools = fhirTools.getMCPToolSchemas();
        return res.json({ jsonrpc: "2.0", id, result: { tools } });
      }

      case "tools/call": {
        const toolName = params?.name as string;
        const toolInput = (params?.arguments ?? {}) as Record<string, unknown>;

        if (!toolName) {
          return res.json({
            jsonrpc: "2.0",
            id,
            error: { code: -32602, message: "Missing tool name" },
          });
        }

        const result = await fhirTools.executeTool(toolName, toolInput, reqHeaders);
        return res.json({ jsonrpc: "2.0", id, result });
      }

      case "context/get": {
        const contextId = params?.contextId as string;
        if (!contextId) {
          return res.json({
            jsonrpc: "2.0",
            id,
            error: { code: -32602, message: "Missing contextId" },
          });
        }
        const context = await fhirTools.getContext(contextId, reqHeaders);
        return res.json({ jsonrpc: "2.0", id, result: context });
      }

      default:
        return res.json({
          jsonrpc: "2.0",
          id,
          error: { code: -32601, message: `Method not found: ${method}` },
        });
    }
  } catch (error: unknown) {
    const detail = error instanceof Error ? error.message : "Unknown error";
    console.error(`RPC error for method ${method}:`, detail);
    return res.json({
      jsonrpc: "2.0",
      id,
      error: { code: -32603, message: "Internal error" },
    });
  }
});

// --- Health Check ---

app.get("/health", (_req, res) => {
  res.json({
    status: "healthy",
    service: "healthclaw-guardrails",
    version: "1.0.0",
    transports: ["streamable-http", "sse", "http-bridge"],
    protocol: "MCP",
    protocolVersion: SUPPORTED_PROTOCOL_VERSIONS[0],
    supportedProtocolVersions: SUPPORTED_PROTOCOL_VERSIONS,
    fhirBaseUrl: FHIR_BASE_URL,
    activeSessions: {
      streamableHttp: streamableSessions.size,
      sse: activeSessions.size,
    },
    cors: {
      mode: ALLOWED_ORIGINS.length > 0 ? "allowlist" : "deny-all",
      allowedOrigins: ALLOWED_ORIGINS.length,
    },
    timestamp: new Date().toISOString(),
  });
});

// --- Start Server ---

if (require.main === module) {
  app.listen(PORT, () => {
    console.error(`FHIR R6 MCP Server v0.9.0 running on port ${PORT}`);
    console.error(`FHIR Base URL: ${FHIR_BASE_URL}`);
    console.error(`Streamable HTTP: http://localhost:${PORT}/mcp`);
    console.error(`SSE endpoint:    http://localhost:${PORT}/sse`);
    console.error(`HTTP bridge:     http://localhost:${PORT}/mcp/rpc`);
    console.error(`CORS: ${ALLOWED_ORIGINS.length > 0 ? `allowlist (${ALLOWED_ORIGINS.join(", ")})` : "deny-all (set ALLOWED_ORIGINS to enable)"}`);
  });
}

export { app };
