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
import { fetchWithTimeout } from "./fetch-timeout";

const { version: SERVER_VERSION } = require("../package.json") as {
  version: string;
};

const app = express();
app.use(express.json());

// Public demo mode: an explicit, opt-in exception to the production auth
// requirement. When MCP_PUBLIC_DEMO is set, the server runs UNAUTHENTICATED but
// is hard-pinned to a single synthetic demo tenant — an open caller can never
// reach a real tenant or bring-your-own PHI. This is how the keyless
// "60-second" demo endpoint runs; the real product server keeps its
// MCP_AUTH_TOKEN and never sets this flag. Read per-request (like
// MCP_AUTH_TOKEN) rather than cached, so config is authoritative at call time.
function isPublicDemo(): boolean {
  return (
    process.env.MCP_PUBLIC_DEMO === "true" || process.env.MCP_PUBLIC_DEMO === "1"
  );
}
function demoTenant(): string {
  return process.env.MCP_DEMO_TENANT || "desktop-demo";
}

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

// --- Phase 3: authorization-server tokens, behind MCP_OAUTH_ENABLED ---
//
// docs/specs/2026-08-16-mcp-authorization.md §3.5, §3.6 and §13.6. A bearer
// that is not MCP_AUTH_TOKEN is a candidate OAuth access token. It is accepted
// only when Flask's introspection says it is live, its audience is exactly
// MCP_CANONICAL_RESOURCE, it carries a read scope and it has not expired.
// Anything else is the same 401 as today. Off (the default), no introspection
// happens and every non-static bearer is refused, which is R7's rollback.
//
// On this path the caller's own credentials never reach Flask: the tenant
// comes from introspection and nowhere else, the Authorization header is
// dropped, the tool-argument overrides are discarded, and the downstream
// credential is a read-scoped step-up token this server mints for the
// consented tenant (tools.ts). Introspection is a hard dependency and fails
// closed: Flask unreachable means 401, never a guess.
export interface OAuthGrant {
  tenantId: string;
  clientId: string;
  scopes: string[];
  tokenHash: string;
  expiresAtMs: number;
}

const INTROSPECTION_CACHE = new Map<string, { grant: OAuthGrant; expiresAtMs: number }>();
const INTROSPECTION_CACHE_TTL_MS = 5 * 60 * 1000;
const TENANT_ID_PATTERN = /^[a-zA-Z0-9_-]{1,64}$/;
//: The internal marker extractHeaders sets on the OAuth path. Never copied
//: from a request, so a client cannot supply it.
export const OAUTH_GRANT_HEADER = "x-oauth-grant";

function oauthEnabled(): boolean {
  const raw = process.env.MCP_OAUTH_ENABLED;
  return raw === "true" || raw === "1";
}

function sha256Hex(value: string): string {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function introspectionURL(): string {
  return `${FHIR_BASE_URL.replace(/\/$/, "")}/oauth/introspect`;
}

// Ask Flask. The answer is trusted only when every one of the conditions
// holds; a missing or odd field is a refusal, not a default. 401 for every
// failure, never 403: a wrong audience is a token that was never for us, and
// 403 would tell the caller their token is recognised here (§3.5).
async function introspect(token: string): Promise<OAuthGrant | null> {
  const clientId = (process.env.MCP_INTROSPECTION_CLIENT_ID || "").trim();
  const clientSecret = (process.env.MCP_INTROSPECTION_CLIENT_SECRET || "").trim();
  const resource = (process.env.MCP_CANONICAL_RESOURCE || "").trim();
  if (!clientId || !clientSecret || !resource) return null;
  let body: Record<string, unknown>;
  try {
    const form = new URLSearchParams({ token, client_id: clientId, client_secret: clientSecret });
    const resp = await fetchWithTimeout(introspectionURL(), {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form.toString(),
    });
    if (!resp.ok) return null;
    body = (await resp.json()) as Record<string, unknown>;
  } catch {
    return null;
  }
  if (!body || body.active !== true) return null;
  if (typeof body.aud !== "string" || body.aud !== resource) return null;
  const scopes = typeof body.scope === "string" ? body.scope.split(/\s+/).filter(Boolean) : [];
  if (!scopes.some((s) => RESOURCE_SCOPES.includes(s))) return null;
  if (typeof body.exp !== "number" || body.exp * 1000 <= Date.now()) return null;
  if (typeof body.tenant_id !== "string" || !TENANT_ID_PATTERN.test(body.tenant_id)) return null;
  return {
    tenantId: body.tenant_id,
    clientId: typeof body.client_id === "string" ? body.client_id : "unknown",
    scopes,
    tokenHash: sha256Hex(token),
    expiresAtMs: body.exp * 1000,
  };
}

// Cached under the token's SHA-256 only, for at most five minutes and never
// past the token's own expiry, so a revoked consent stops being served within
// the window and the token value itself is never held (§9.7).
async function resolveOAuthGrant(token: string): Promise<OAuthGrant | null> {
  const key = sha256Hex(token);
  const now = Date.now();
  const cached = INTROSPECTION_CACHE.get(key);
  if (cached && cached.expiresAtMs > now) return cached.grant;
  INTROSPECTION_CACHE.delete(key);
  const grant = await introspect(token);
  if (!grant) return null;
  const expiresAtMs = Math.min(now + INTROSPECTION_CACHE_TTL_MS, grant.expiresAtMs);
  INTROSPECTION_CACHE.set(key, { grant, expiresAtMs });
  return grant;
}

function resetOAuthStateForTests(): void {
  INTROSPECTION_CACHE.clear();
}

// --- RFC 9728 protected-resource metadata (phase 1, behind the constant) ---
//
// docs/specs/2026-08-16-mcp-authorization.md §3.2/§3.4, as amended 2026-09-02.
// The canonical resource identifier is one pinned string, and everything below
// is derived from it. Unset, none of this runs: the well-known paths 404 and
// the challenge stays a bare `Bearer`, which is today's behaviour. Read
// per-request like MCP_AUTH_TOKEN so config is authoritative at call time;
// assertMCPCanonicalResourceConfigured re-reads it at boot so a malformed value
// refuses to start rather than disabling the feature in silence.
//
// This admits nobody. It adds two unauthenticated documents that hold no
// secret, name no tenant and grant nothing, and two parameters on a refusal
// that is still a refusal.
const AUTHORIZATION_SERVER_ISSUER = "https://app.healthclaw.io";
const RESOURCE_SCOPES = ["fhir.read", "context.read"];
const PRM_BASE_PATH = "/.well-known/oauth-protected-resource";

// The check that matters is not "is this a URL" but "will the document we serve
// survive RFC 9728 §3.3". A client derives the resource identifier by removing
// the well-known segment from the URL it fetched, and rejects the document if
// `resource` is not that string. So the configured value must already BE the
// identifier a client derives, character for character — the constant is
// advertised verbatim as `resource`, while the URL that carries it is built
// from the parsed origin and path.
//
// Nine shapes parse as https URLs and fail that: a host or scheme in mixed case
// (the URL parser lowercases both), an explicit `:443` (dropped), a `?query` or
// `#fragment` (not in the path), `user:pass@` userinfo (also a credential we
// would then publish in an unauthenticated, CORS-open document), a bare
// trailing slash, and dot segments (resolved). Each one boots a server whose
// own challenge points at a document the challenge's reader will refuse — the
// §9.3 "confidently wrong" mode, which is the failure this check exists to
// catch. Verified by starting the server on each value and fetching the
// document at the URL its own challenge advertises.
function parseCanonicalResource(raw: string | undefined): URL | null {
  if (!raw || !raw.trim()) return null;
  const trimmed = raw.trim();
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:" || !parsed.hostname) return null;
  // `origin` re-serializes: lowercased scheme and host, no userinfo, no default
  // port. `pathname` is normalized and excludes query and fragment. A root path
  // contributes nothing, because protectedResourceMetadataURL appends nothing
  // for it and the identifier a client derives therefore has no trailing slash.
  const canonicalForm =
    parsed.pathname === "/" ? parsed.origin : `${parsed.origin}${parsed.pathname}`;
  if (trimmed !== canonicalForm) return null;
  return parsed;
}

function canonicalResource(): URL | null {
  return parseCanonicalResource(process.env.MCP_CANONICAL_RESOURCE);
}

// RFC 9728 §3.1 inserts the resource's path after the well-known segment, so
// `https://host/mcp` is described at `<host>/.well-known/oauth-protected-resource/mcp`.
// Built from the constant and never from req.hostname. RFC 9728 §3.3 has a
// client reject a document whose `resource` is not the identifier it inserted
// into the well-known path, so what we advertise cannot follow the request —
// whatever any proxy in front of us does or does not rewrite.
function protectedResourceMetadataURL(resource: URL): string {
  const path = resource.pathname === "/" ? "" : resource.pathname;
  return `${resource.origin}${PRM_BASE_PATH}${path}`;
}

// `resourceIdentifier` is the configured string as configured, not a
// re-serialized URL, so it string-equals the audience the client will request
// and the authorization server will record. Nothing here comes from the
// request.
function protectedResourceMetadata(resourceIdentifier: string): Record<string, unknown> {
  return {
    resource: resourceIdentifier,
    authorization_servers: [AUTHORIZATION_SERVER_ISSUER],
    scopes_supported: RESOURCE_SCOPES,
    bearer_methods_supported: ["header"],
    resource_name: "HealthClaw Guardrails",
    resource_documentation: `${AUTHORIZATION_SERVER_ISSUER}/r6/fhir/docs/privacy-policy`,
  };
}

// A document whose `resource` disagrees with the host it was fetched from is
// rejected by a conformant client (RFC 9728 §3.3), and that reads to a partner
// as our bug. Until DNS points the canonical name here, the platform hostname
// keeps answering exactly as it does today.
function hostIsCanonical(req: express.Request, resource: URL): boolean {
  const host = req.headers.host;
  if (typeof host !== "string" || !host) return false;
  try {
    return new URL(`https://${host}`).hostname === resource.hostname.toLowerCase();
  } catch {
    return false;
  }
}

function challengeHeader(req: express.Request): string {
  const resource = canonicalResource();
  if (!resource || !hostIsCanonical(req, resource)) return "Bearer";

  const authorization = req.headers.authorization;
  const credentialPresented =
    typeof authorization === "string" && authorization.trim().length > 0;
  // RFC 6750 §3.1: SHOULD NOT send `error` when no credential was presented —
  // there is no token to call invalid. The description below distinguishes
  // nothing on purpose: expired, wrong audience and not-a-token recover
  // identically, and telling them apart is an oracle for a caller with no
  // business getting one. It never carries the credential or a tenant.
  const params = credentialPresented
    ? [
        'error="invalid_token"',
        'error_description="The access token is invalid, expired, or was not issued for this resource."',
      ]
    : [];
  params.push(`resource_metadata="${protectedResourceMetadataURL(resource)}"`);
  params.push(`scope="${RESOURCE_SCOPES.join(" ")}"`);
  return `Bearer ${params.join(", ")}`;
}

// Health probes and CORS preflight remain public. When configured, every MCP
// network transport requires the deployment-scoped bearer credential.
app.use(async (req, res, next) => {
  const expectedToken = process.env.MCP_AUTH_TOKEN;
  if (
    req.method === "OPTIONS" ||
    !expectedToken ||
    isPublicDemo() ||
    !isMCPTransportPath(req.path)
  ) {
    return next();
  }

  const authorization = req.headers.authorization;
  if (isMCPBearerCredential(authorization)) return next();

  const bearer = /^Bearer (.+)$/i.exec(authorization || "");
  if (bearer && oauthEnabled()) {
    const grant = await resolveOAuthGrant(bearer[1].trim());
    if (grant) {
      res.locals.oauthGrant = grant;
      return next();
    }
  }

  // The refusal is readable from a browser context (§9.4): it carries no
  // secret, names no tenant and grants nothing, and a 401 a browser can read
  // is still a 401. The transport's own origin allowlist is untouched.
  res.setHeader("WWW-Authenticate", challengeHeader(req));
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Expose-Headers", "WWW-Authenticate");
  return res.status(401).json({ error: "Unauthorized" });
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
  res.setHeader("Access-Control-Expose-Headers", "Mcp-Session-Id, WWW-Authenticate");
  if (req.method === "OPTIONS") {
    // A browser preflights POST /mcp; without an allow-origin here it never
    // sends the request and never reads the 401 that says where to go
    // (§9.4, part 2 of #523). Answering `*` on the preflight lets the request
    // through to a refusal it can read; the actual response still carries an
    // allow-origin only for an allowlisted origin, so nothing the transport
    // returns becomes readable by any other page.
    if (isMCPTransportPath(req.path) && !res.getHeader("Access-Control-Allow-Origin")) {
      res.setHeader("Access-Control-Allow-Origin", "*");
    }
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

function extractHeaders(
  req: express.Request,
  grant?: OAuthGrant
): Record<string, string> {
  const h: Record<string, string> = {};
  if (grant) {
    // §3.6: on the OAuth path the tenant comes from introspection and from
    // nowhere else. The inbound Authorization header, any step-up token and
    // the bring-your-own-FHIR (SHARP) headers are all dropped: a consented
    // connection reaches one tenant through the guardrails and nothing else.
    h["x-tenant-id"] = grant.tenantId;
    h["x-agent-id"] = `oauth:${grant.clientId}`;
    h[OAUTH_GRANT_HEADER] = JSON.stringify({
      tenantId: grant.tenantId,
      tokenHash: grant.tokenHash,
      expiresAtMs: grant.expiresAtMs,
    });
    return h;
  }
  if (isPublicDemo()) {
    // Open demo endpoint: pin to the synthetic demo tenant and ignore every
    // client-supplied tenant, step-up, or bring-your-own-FHIR header, so an
    // unauthenticated caller can only ever reach synthetic demo data.
    h["x-tenant-id"] = demoTenant();
    return h;
  }
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

const CREDENTIAL_ARGUMENTS = [
  "_tenantId",
  "_stepUpToken",
  "_authorization",
  "_fhirServerUrl",
  "_fhirAccessToken",
  "_patientId",
];

function stripCredentialArguments(toolArgs: Record<string, unknown>): void {
  for (const key of CREDENTIAL_ARGUMENTS) delete toolArgs[key];
}

// The SSE transport lets a client that cannot set HTTP headers pass tenant,
// step-up, bearer and SHARP context as underscored tool arguments. On the
// OAuth path (a session whose headers carry the grant marker) none of them is
// honoured — they are removed, not applied — exactly as the public demo pins
// them. Pure, so the property is asserted directly (R8 for this transport).
function applyToolArgumentOverrides(
  toolArgs: Record<string, unknown>,
  sessionHeaders: Record<string, string>
): Record<string, string> {
  const toolHeaders: Record<string, string> = { ...sessionHeaders };
  if (sessionHeaders[OAUTH_GRANT_HEADER]) {
    stripCredentialArguments(toolArgs);
    return toolHeaders;
  }
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
  return toolHeaders;
}

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
    // Tool-arg headers (_tenantId, _stepUpToken, _authorization, and the
    // SHARP trio) override session headers, allowing per-call overrides
    // without changing the connection — except on the OAuth path, where
    // they are discarded (P3-a, R8).
    const toolHeaders = applyToolArgumentOverrides(toolArgs, sessionHeaders);

    // Demo mode overrides every client-supplied header (incl. _tenantId tool
    // args) with the pinned synthetic tenant — an open caller stays boxed in.
    const finalHeaders = isPublicDemo()
      ? { "x-tenant-id": demoTenant() }
      : toolHeaders;
    return executeMCPTool(fhirTools, name, toolArgs, finalHeaders);
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

  const reqHeaders = extractHeaders(req, res.locals.oauthGrant as OAuthGrant | undefined);
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
        // Require valid session for tool calls.
        //
        // The two failures below are NOT the same failure, and answering both
        // with 400 is what turned a routine session expiry into a dead
        // conversation for the first clinician to use this.
        //
        // Sessions live in an in-memory Map with a 30-minute idle TTL, so they
        // end constantly: every redeploy, every restart, every user who leaves
        // a chat open over lunch. Per the Streamable HTTP spec a server that
        // does not recognise an Mcp-Session-Id MUST answer 404, and a client
        // that receives 404 MUST re-initialise. That handshake is the entire
        // recovery mechanism, and 400 does not trigger it — the client reports
        // an execution error and stops.
        //
        // What made it hard to see: tools/list requires no session, so the
        // tool list kept rendering while every call failed. It looks like a
        // broken deployment and is a expired cookie.
        if (requestSessionId && !existingSession) {
          return res.status(404).json({
            jsonrpc: "2.0",
            id,
            error: {
              code: -32600,
              message:
                "Unknown or expired session. Re-initialize to obtain a new " +
                "Mcp-Session-Id, then retry.",
            },
          });
        }
        // No session id at all: the client never initialised. The spec calls
        // for 400 here, and there is nothing to recover — re-initialising is
        // what it should have done first.
        if (!existingSession) {
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
        if (reqHeaders[OAUTH_GRANT_HEADER]) stripCredentialArguments(toolInput);

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
  credential: string;
}>();

// An SSE session captures its headers at connect and every later POST to
// /messages rides on them, so the session is bound to the credential that
// opened it: the static token, or one OAuth token's hash. A request that
// authenticates as anything else is refused, or it would execute as the
// tenant the session was opened for. Streamable HTTP needs no such binding:
// its headers are resolved per request.
function sessionCredential(grant: OAuthGrant | undefined): string {
  return grant ? `oauth:${grant.tokenHash}` : "static";
}

app.get("/sse", async (req, res) => {
  // Capture headers from the SSE connection request and pass them into the
  // server instance so CallToolRequestSchema forwwards X-Tenant-ID on every tool call.
  const reqHeaders = extractHeaders(req, res.locals.oauthGrant as OAuthGrant | undefined);
  const server = createMCPServer(reqHeaders);
  const transport = new SSEServerTransport("/messages", res);
  activeSessions.set(transport.sessionId, {
    transport,
    headers: reqHeaders,
    lastActivity: Date.now(),
    credential: sessionCredential(res.locals.oauthGrant as OAuthGrant | undefined),
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
  if (session.credential !== sessionCredential(res.locals.oauthGrant as OAuthGrant | undefined)) {
    return res.status(401).json({ error: "Unauthorized" });
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
  const reqHeaders = extractHeaders(req, res.locals.oauthGrant as OAuthGrant | undefined);

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

// --- Protected-resource metadata (RFC 9728) ---
//
// Mounted rather than declared per-path so the sub-path form always matches the
// path inside the constant, which is the same path the challenge advertises. A
// header pointing at a URL this server does not serve is the failure mode the
// spec's §9.3 calls the worst one: confidently wrong beats absent, for the
// partner reading the error.
//
// Both forms are served because clients try the sub-path first and the root
// second. `next()` on every other case leaves the response byte-identical to
// today's 404.
app.use(PRM_BASE_PATH, (req, res, next) => {
  if (req.method !== "GET" && req.method !== "HEAD") return next();

  const resource = canonicalResource();
  if (!resource || !hostIsCanonical(req, resource)) return next();

  const isPathInsertionForm = req.path === resource.pathname;
  const isRootForm = req.path === "/";
  if (!isPathInsertionForm && !isRootForm) return next();

  // Unauthenticated and readable cross-origin. A document a client must read
  // before it can authenticate cannot itself require authentication, and a
  // browser-context client that cannot read it is back at the dead end this
  // phase removes (spec §9.4). The transport endpoint's origin allowlist is
  // untouched.
  res.setHeader("Access-Control-Allow-Origin", "*");
  return res.json(
    protectedResourceMetadata((process.env.MCP_CANONICAL_RESOURCE || "").trim())
  );
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
    oauth: { enabled: oauthEnabled() },
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
  const demo =
    env.MCP_PUBLIC_DEMO === "true" || env.MCP_PUBLIC_DEMO === "1";
  if (env.NODE_ENV === "production" && !env.MCP_AUTH_TOKEN?.trim() && !demo) {
    throw new Error(
      "MCP_AUTH_TOKEN is required when NODE_ENV=production " +
        "(or set MCP_PUBLIC_DEMO=true to run an unauthenticated, demo-tenant-only server)"
    );
  }
}

// A value that cannot be an audience is a configuration error, and the failure
// it would otherwise produce is silence: the routes 404 and the challenge stays
// bare, so phase 1 looks deployed and is not.
function assertMCPCanonicalResourceConfigured(
  env: NodeJS.ProcessEnv = process.env
): void {
  const raw = env.MCP_CANONICAL_RESOURCE;
  if (!raw || !raw.trim()) return; // Unset is the supported off state.
  if (!parseCanonicalResource(raw)) {
    // Names the variable and the shape, never the value: the rejected value may
    // be the reason it was rejected (userinfo credentials), and a boot crash is
    // the loudest place in the system to print one.
    throw new Error(
      "MCP_CANONICAL_RESOURCE must be an absolute https:// URL in canonical " +
        "form — lowercase scheme and host, no userinfo, no default port, no " +
        "query, no fragment, no trailing slash on a root path " +
        "(e.g. https://mcp.healthclaw.io/mcp)"
    );
  }
}

// Enabled means every piece it depends on is present, or the server does not
// start: a half-configured OAuth path would refuse every connector token in
// production and look, from outside, like a deploy that worked (§7).
function assertMCPOAuthConfigured(env: NodeJS.ProcessEnv = process.env): void {
  const raw = env.MCP_OAUTH_ENABLED;
  if (raw !== "true" && raw !== "1") return;
  const missing = [
    "MCP_CANONICAL_RESOURCE",
    "MCP_INTROSPECTION_CLIENT_ID",
    "MCP_INTROSPECTION_CLIENT_SECRET",
    "INTERNAL_TOKEN_MINT_SECRET",
  ].filter((name) => !env[name]?.trim());
  if (missing.length > 0 || !parseCanonicalResource(env.MCP_CANONICAL_RESOURCE)) {
    throw new Error(
      "MCP_OAUTH_ENABLED=true requires a canonical MCP_CANONICAL_RESOURCE, " +
        "MCP_INTROSPECTION_CLIENT_ID, MCP_INTROSPECTION_CLIENT_SECRET and " +
        `INTERNAL_TOKEN_MINT_SECRET (missing: ${missing.join(", ") || "none, but the resource is not canonical"})`
    );
  }
}

if (require.main === module) {
  assertMCPAuthConfigured();
  assertMCPCanonicalResourceConfigured();
  assertMCPOAuthConfigured();
  app.listen(PORT, () => {
    console.error(`FHIR R6 MCP Server v${SERVER_VERSION} running on port ${PORT}`);
    console.error(`FHIR Base URL: ${FHIR_BASE_URL}`);
    console.error(`Streamable HTTP: http://localhost:${PORT}/mcp`);
    console.error(`SSE endpoint:    http://localhost:${PORT}/sse`);
    console.error(`HTTP bridge:     http://localhost:${PORT}/mcp/rpc`);
    console.error(`CORS: ${ALLOWED_ORIGINS.length > 0 ? `allowlist (${ALLOWED_ORIGINS.join(", ")})` : "deny-all (set ALLOWED_ORIGINS to enable)"}`);
    if (isPublicDemo()) {
      console.error(`PUBLIC DEMO MODE: unauthenticated, hard-pinned to synthetic tenant '${demoTenant()}'`);
    }
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
  assertMCPCanonicalResourceConfigured,
  assertMCPOAuthConfigured,
  applyToolArgumentOverrides,
  resetOAuthStateForTests,
  sessionCredential,
  cleanupExpiredRuntimeStateForTests,
  closeMCPServerForTests,
  getRuntimeStateForTests,
  SERVER_VERSION,
};
