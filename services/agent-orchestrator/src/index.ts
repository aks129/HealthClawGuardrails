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
  McpError,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import crypto from "crypto";
import { FHIRTools } from "./tools";
import { executeMCPTool } from "./mcp-tool-result";

const { version: SERVER_VERSION } = require("../package.json") as {
  version: string;
};

const app = express();
app.use(express.json());

// Minimal request access log so we can see which probes from marketplace
// platforms (PromptOpinion, Devpost reviewers, Claude Desktop) actually reach
// us. Logs to stderr only; bodies are NOT logged.
app.use((req, _res, next) => {
  const origin = req.headers.origin || "-";
  const ua = (req.headers["user-agent"] || "-").toString().slice(0, 80);
  const ct = req.headers["content-type"] || "-";
  console.error(`[req] ${req.method} ${req.url} origin=${origin} ct=${ct} ua=${ua}`);
  next();
});

function isMCPTransportPath(path: string): boolean {
  const normalizedPath = path.toLowerCase();
  return (
    normalizedPath === "/mcp" ||
    normalizedPath.startsWith("/mcp/") ||
    normalizedPath === "/sse" ||
    normalizedPath.startsWith("/sse/") ||
    normalizedPath === "/messages" ||
    normalizedPath.startsWith("/messages/")
  );
}

function tokenMatches(actual: string, expected: string): boolean {
  const actualBytes = Buffer.from(actual);
  const expectedBytes = Buffer.from(expected);
  return (
    actualBytes.length === expectedBytes.length &&
    crypto.timingSafeEqual(actualBytes, expectedBytes)
  );
}

function isMCPBearerCredential(authorization: string | undefined): boolean {
  const expectedToken = process.env.MCP_AUTH_TOKEN;
  const match = /^Bearer (.+)$/i.exec(authorization || "");
  return Boolean(
    expectedToken && match && tokenMatches(match[1], expectedToken)
  );
}

// Health probes and CORS preflight remain public. When configured, every MCP
// network transport requires the deployment-scoped bearer credential.
app.use((req, res, next) => {
  const expectedToken = process.env.MCP_AUTH_TOKEN;
  if (
    req.method === "OPTIONS" ||
    !expectedToken ||
    !isMCPTransportPath(req.path)
  ) {
    return next();
  }

  if (!isMCPBearerCredential(req.headers.authorization)) {
    res.setHeader("WWW-Authenticate", "Bearer");
    return res.status(401).json({ error: "Unauthorized" });
  }
  next();
});

// Railway / Heroku / Fly inject PORT; honor that first so the platform's
// public proxy can reach us, then fall back to the explicit MCP_PORT, then
// the default development port.
const PORT = process.env.PORT || process.env.MCP_PORT || 3001;
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
    "Content-Type, Authorization, X-Tenant-Id, X-Step-Up-Token, X-Agent-Id, X-Human-Confirmed, Mcp-Session-Id, X-FHIR-Server-URL, X-FHIR-Access-Token, X-Patient-ID, X-FHIR-Refresh-Token, X-FHIR-Refresh-Url"
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
const SESSION_TTL_MS = 30 * 60 * 1000;

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
  if (typeof auth === "string" && !isMCPBearerCredential(auth)) {
    h["authorization"] = auth;
  }
  const humanConfirmed = req.headers["x-human-confirmed"];
  if (typeof humanConfirmed === "string") h["x-human-confirmed"] = humanConfirmed;
  // SHARP-on-MCP context headers (Standardised Healthcare Agent Remote Protocol).
  // The agent host forwards the FHIR base URL + SMART access token on every call;
  // this server propagates them to Flask which builds a per-request upstream proxy.
  const fhirServerUrl = req.headers["x-fhir-server-url"];
  if (typeof fhirServerUrl === "string") h["x-fhir-server-url"] = fhirServerUrl;
  const fhirAccessToken = req.headers["x-fhir-access-token"];
  if (typeof fhirAccessToken === "string") h["x-fhir-access-token"] = fhirAccessToken;
  const patientId = req.headers["x-patient-id"];
  if (typeof patientId === "string") h["x-patient-id"] = patientId;
  // Optional refresh-token headers (PromptOpinion sends these when the agent
  // host authorized offline_access). Forwarded but not yet acted on.
  const refreshToken = req.headers["x-fhir-refresh-token"];
  if (typeof refreshToken === "string") h["x-fhir-refresh-token"] = refreshToken;
  const refreshUrl = req.headers["x-fhir-refresh-url"];
  if (typeof refreshUrl === "string") h["x-fhir-refresh-url"] = refreshUrl;
  return h;
}

// --- MCP Server Factory (creates per-session server instances) ---
//
// sessionHeaders: HTTP request headers captured when the session was initiated.
// For SSE these come from the GET /sse connection; for Streamable HTTP the
// tools/call handler re-extracts headers per-request and bypasses this factory,
// so sessionHeaders is only meaningfully used on the SSE path.

// FHIR context advertisement.
//
// Two parallel declarations so both ecosystems auto-detect compliance:
//
//   1. SHARP-on-MCP (https://sharponmcp.com) — vendor-neutral. Lives under
//      capabilities.experimental.{fhir_context_required, sharp}.
//
//   2. PromptOpinion FHIR extension
//      (https://docs.promptopinion.ai/fhir-context/mcp-fhir-context) — lives
//      under capabilities.extensions["ai.promptopinion/fhir-context"]. The
//      "scopes" array declares the SMART-on-FHIR scopes Po should request
//      from the agent host when launching us.
//
// Both specs converge on the same headers (X-FHIR-Server-URL,
// X-FHIR-Access-Token, X-Patient-ID, optionally X-FHIR-Refresh-Token /
// X-FHIR-Refresh-Url) so the underlying request flow is identical.
const SHARP_CAPABILITIES = {
  tools: {},
  logging: {},
  extensions: {
    "ai.promptopinion/fhir-context": {
      scopes: [
        { name: "patient/*.read", required: true },
        { name: "patient/*.write", required: false },
        { name: "offline_access", required: false },
      ],
    },
  },
  experimental: {
    fhir_context_required: { required: true },
    sharp: {
      version: "1.0",
      headers: ["X-FHIR-Server-URL", "X-FHIR-Access-Token", "X-Patient-ID"],
      spec: "https://sharponmcp.com",
    },
  },
};

function createMCPServer(sessionHeaders: Record<string, string> = {}): Server {
  const server = new Server(
    { name: "healthclaw-guardrails", version: SERVER_VERSION },
    { capabilities: SHARP_CAPABILITIES }
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
    // SHARP-on-MCP tool-arg overrides (Claude Desktop & stdio clients can't
    // set HTTP headers, so they pass SHARP context as underscored tool args).
    if (typeof toolArgs._fhirServerUrl === "string") {
      toolHeaders["x-fhir-server-url"] = toolArgs._fhirServerUrl as string;
      delete toolArgs._fhirServerUrl;
    }
    if (typeof toolArgs._fhirAccessToken === "string") {
      toolHeaders["x-fhir-access-token"] = toolArgs._fhirAccessToken as string;
      delete toolArgs._fhirAccessToken;
    }
    if (typeof toolArgs._patientId === "string") {
      toolHeaders["x-patient-id"] = toolArgs._patientId as string;
      delete toolArgs._patientId;
    }

    return executeMCPTool(fhirTools, name, toolArgs, toolHeaders);
  });

  return server;
}

// --- Streamable HTTP Transport (preferred — /mcp endpoint) ---

interface StreamableSession {
  server: Server;
  lastActivity: number;
}

const streamableSessions = new Map<string, StreamableSession>();

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
  const requestSessionId = req.headers["mcp-session-id"] as string | undefined;
  const existingSession = requestSessionId
    ? streamableSessions.get(requestSessionId)
    : undefined;
  if (existingSession) existingSession.lastActivity = Date.now();

  try {
    switch (method) {
      case "initialize": {
        // Server ALWAYS generates session ID (prevent session fixation)
        const sessionId = crypto.randomUUID();
        const server = createMCPServer();
        streamableSessions.set(sessionId, { server, lastActivity: Date.now() });

        // Protocol version negotiation
        const clientVersion = params?.protocolVersion as string | undefined;
        const negotiatedVersion = negotiateProtocolVersion(clientVersion);

        res.setHeader("Mcp-Session-Id", sessionId);
        return res.json({
          jsonrpc: "2.0",
          id,
          result: {
            protocolVersion: negotiatedVersion,
            capabilities: SHARP_CAPABILITIES,
            serverInfo: { name: "healthclaw-guardrails", version: SERVER_VERSION },
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
        if (!requestSessionId || !existingSession) {
          return res.status(400).json({
            jsonrpc: "2.0",
            id,
            error: { code: -32600, message: "Invalid or missing session. Call initialize first." },
          });
        }

        if (!params || typeof params !== "object" || Array.isArray(params)) {
          return res.json({
            jsonrpc: "2.0",
            id,
            error: { code: -32602, message: "Invalid tool call parameters" },
          });
        }

        const toolName = params.name as string;
        const rawToolInput = params.arguments;

        if (typeof toolName !== "string" || !toolName) {
          return res.json({
            jsonrpc: "2.0",
            id,
            error: { code: -32602, message: "Missing tool name" },
          });
        }

        if (
          rawToolInput !== undefined &&
          (!rawToolInput || typeof rawToolInput !== "object" || Array.isArray(rawToolInput))
        ) {
          return res.json({
            jsonrpc: "2.0",
            id,
            error: { code: -32602, message: "Tool arguments must be an object" },
          });
        }
        const toolInput = (rawToolInput ?? {}) as Record<string, unknown>;

        const result = await executeMCPTool(
          fhirTools,
          toolName,
          toolInput,
          reqHeaders
        );
        return res.json({
          jsonrpc: "2.0",
          id,
          result,
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
    if (error instanceof McpError) {
      return res.json({
        jsonrpc: "2.0",
        id,
        error: { code: error.code, message: error.message },
      });
    }
    const detail = error instanceof Error ? error.message : "Unknown error";
    console.error("Streamable HTTP error for method:", method, "-", detail);
    return res.json({
      jsonrpc: "2.0",
      id,
      error: { code: -32603, message: "Internal error" },
    });
  }
});

// GET /mcp — MCP Streamable HTTP spec says servers that don't expose a
// server-to-client SSE stream on this endpoint MUST return 405 (not 404)
// so spec-strict clients (PromptOpinion, MCP Inspector, etc.) can tell the
// channel is intentionally unsupported and continue with POST-only.
app.get("/mcp", (_req, res) => {
  res.setHeader("Allow", "POST, DELETE, OPTIONS");
  res.status(405).json({
    jsonrpc: "2.0",
    error: {
      code: -32000,
      message: "GET not supported on this MCP endpoint. Use POST for client-to-server JSON-RPC; DELETE for session cleanup.",
    },
  });
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
function cleanupExpiredRuntimeState(now: number = Date.now()): void {
  for (const [sessionId, session] of streamableSessions) {
    if (now - session.lastActivity > SESSION_TTL_MS) {
      streamableSessions.delete(sessionId);
    }
  }

  for (const [sessionId, session] of activeSessions) {
    if (now - session.lastActivity > SESSION_TTL_MS) {
      activeSessions.delete(sessionId);
      void session.transport.close().catch((error: unknown) => {
        console.error("Failed to close expired SSE session:", error);
      });
    }
  }

  for (const [clientIp, bucket] of rateLimitMap) {
    if (now > bucket.resetAt) rateLimitMap.delete(clientIp);
  }

  const MAX_SESSIONS = 1000;
  if (streamableSessions.size > MAX_SESSIONS) {
    const iterator = streamableSessions.keys();
    const toDelete = streamableSessions.size - MAX_SESSIONS;
    for (let i = 0; i < toDelete; i++) {
      const key = iterator.next().value;
      if (key) streamableSessions.delete(key);
    }
  }
}

const sessionCleanupInterval = setInterval(cleanupExpiredRuntimeState, 60_000);
sessionCleanupInterval.unref?.();

// --- SSE Transport (legacy MCP, still supported) ---

const activeSessions = new Map<string, {
  transport: SSEServerTransport;
  headers: Record<string, string>;
  lastActivity: number;
}>();

app.get("/sse", async (req, res) => {
  // Capture headers from the SSE connection request and pass them into the
  // server instance so CallToolRequestSchema forwwards X-Tenant-ID on every tool call.
  const reqHeaders = extractHeaders(req);
  const server = createMCPServer(reqHeaders);
  const transport = new SSEServerTransport("/messages", res);
  activeSessions.set(transport.sessionId, {
    transport,
    headers: reqHeaders,
    lastActivity: Date.now(),
  });

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
  session.lastActivity = Date.now();
  await session.transport.handlePostMessage(req, res, req.body);
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
    console.error("RPC error for method:", method, "-", detail);
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
    version: SERVER_VERSION,
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
    sharp: {
      compliant: true,
      version: "1.0",
      headers: ["X-FHIR-Server-URL", "X-FHIR-Access-Token", "X-Patient-ID"],
      spec: "https://sharponmcp.com",
    },
    timestamp: new Date().toISOString(),
  });
});

// --- Start Server ---

function assertMCPAuthConfigured(env: NodeJS.ProcessEnv = process.env): void {
  if (env.NODE_ENV === "production" && !env.MCP_AUTH_TOKEN?.trim()) {
    throw new Error("MCP_AUTH_TOKEN is required when NODE_ENV=production");
  }
}

if (require.main === module) {
  assertMCPAuthConfigured();
  app.listen(PORT, () => {
    console.error(`FHIR R6 MCP Server v${SERVER_VERSION} running on port ${PORT}`);
    console.error(`FHIR Base URL: ${FHIR_BASE_URL}`);
    console.error(`Streamable HTTP: http://localhost:${PORT}/mcp`);
    console.error(`SSE endpoint:    http://localhost:${PORT}/sse`);
    console.error(`HTTP bridge:     http://localhost:${PORT}/mcp/rpc`);
    console.error(`CORS: ${ALLOWED_ORIGINS.length > 0 ? `allowlist (${ALLOWED_ORIGINS.join(", ")})` : "deny-all (set ALLOWED_ORIGINS to enable)"}`);
  });
}

function closeMCPServerForTests(): void {
  clearInterval(sessionCleanupInterval);
  rateLimitMap.clear();
  streamableSessions.clear();
  activeSessions.clear();
}

function cleanupExpiredRuntimeStateForTests(now: number): void {
  cleanupExpiredRuntimeState(now);
}

function getRuntimeStateForTests(): {
  streamableSessions: number;
  rateLimitBuckets: number;
} {
  return {
    streamableSessions: streamableSessions.size,
    rateLimitBuckets: rateLimitMap.size,
  };
}

export {
  app,
  assertMCPAuthConfigured,
  cleanupExpiredRuntimeStateForTests,
  closeMCPServerForTests,
  getRuntimeStateForTests,
  SERVER_VERSION,
};
