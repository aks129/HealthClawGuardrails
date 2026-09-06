/**
 * Phase 3: the MCP server accepts authorization-server tokens (spec §3.5,
 * §3.6, §7, §8.2 R1–R8, §13.6), behind MCP_OAUTH_ENABLED.
 *
 * Flask is a mock here (introspection, the read-scoped mint, and the FHIR
 * read itself), so every assertion is about what this server sends and
 * refuses: which tenant reaches Flask, which credential, and that nothing a
 * caller supplied does. The invariant of §7 is asserted at every step: an
 * unauthenticated initialize never returns anything but 401.
 */
import http from "http";
import { AddressInfo } from "net";
import request from "supertest";

jest.mock("node-fetch", () => jest.fn());
import fetch from "node-fetch";
import {
  app,
  applyToolArgumentOverrides,
  assertMCPOAuthConfigured,
  closeMCPServerForTests,
  resetOAuthStateForTests,
  sessionCredential,
} from "./index";
import { resetGrantReadTokenCacheForTests } from "./tools";

const mockFetch = fetch as unknown as jest.Mock;

const CANONICAL = "https://mcp.healthclaw.io/mcp";
const CANONICAL_HOST = "mcp.healthclaw.io";
const FHIR_RESOURCE = "https://app.healthclaw.io/r6/fhir";
const STATIC_TOKEN = "static-mcp-token-for-tests";
const OAUTH_TOKEN = "an-opaque-access-token-from-flask";
const TENANT = "ca-real-tenant";

interface Recorded {
  url: string;
  init: Record<string, any>;
}

let calls: Recorded[] = [];
let introspection: Record<string, unknown> | (() => never);

function respond(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: { get: () => null },
  };
}

function installFlask() {
  mockFetch.mockImplementation(async (url: string, init: Record<string, any> = {}) => {
    calls.push({ url, init });
    if (url.endsWith("/oauth/introspect")) {
      if (typeof introspection === "function") return introspection();
      return respond(200, introspection);
    }
    if (url.endsWith("/internal/step-up-token")) {
      const body = JSON.parse(init.body || "{}");
      return respond(200, { token: `read-token-for-${body.tenant_id}`, tenant_id: body.tenant_id, scope: body.scope ?? null });
    }
    return respond(200, { resourceType: "Patient", id: "p-1" });
  });
}

function liveIntrospection(overrides: Record<string, unknown> = {}) {
  return {
    active: true,
    token_type: "Bearer",
    aud: CANONICAL,
    scope: "fhir.read context.read",
    tenant_id: TENANT,
    client_id: "cid-claude",
    exp: Math.floor(Date.now() / 1000) + 3600,
    ...overrides,
  };
}

const savedEnv: Record<string, string | undefined> = {};
const ENV_KEYS = [
  "MCP_CANONICAL_RESOURCE",
  "MCP_AUTH_TOKEN",
  "MCP_PUBLIC_DEMO",
  "MCP_OAUTH_ENABLED",
  "MCP_INTROSPECTION_CLIENT_ID",
  "MCP_INTROSPECTION_CLIENT_SECRET",
  "INTERNAL_TOKEN_MINT_SECRET",
  "ALLOWED_ORIGINS",
];

beforeEach(() => {
  for (const key of ENV_KEYS) savedEnv[key] = process.env[key];
  delete process.env.MCP_PUBLIC_DEMO;
  process.env.MCP_AUTH_TOKEN = STATIC_TOKEN;
  process.env.MCP_CANONICAL_RESOURCE = CANONICAL;
  process.env.MCP_OAUTH_ENABLED = "true";
  process.env.MCP_INTROSPECTION_CLIENT_ID = "mcp-server";
  process.env.MCP_INTROSPECTION_CLIENT_SECRET = "introspection-secret";
  process.env.INTERNAL_TOKEN_MINT_SECRET = "mint-secret";
  calls = [];
  introspection = liveIntrospection();
  mockFetch.mockReset();
  installFlask();
  resetOAuthStateForTests();
  resetGrantReadTokenCacheForTests();
});

afterEach(() => {
  for (const [key, value] of Object.entries(savedEnv)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
});

afterAll(() => {
  closeMCPServerForTests();
});

function initialize(authorization?: string) {
  const req = request(app).post("/mcp").set("Host", CANONICAL_HOST);
  if (authorization) req.set("Authorization", authorization);
  return req.send({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "t", version: "0" } },
  });
}

async function session(): Promise<string> {
  const res = await initialize(`Bearer ${OAUTH_TOKEN}`);
  expect(res.status).toBe(200);
  return res.headers["mcp-session-id"];
}

function call(sessionId: string, name: string, args: Record<string, unknown>, authorization = `Bearer ${OAUTH_TOKEN}`) {
  return request(app)
    .post("/mcp")
    .set("Host", CANONICAL_HOST)
    .set("Authorization", authorization)
    .set("Mcp-Session-Id", sessionId)
    .send({ jsonrpc: "2.0", id: 2, method: "tools/call", params: { name, arguments: args } });
}

const introspections = () => calls.filter((c) => c.url.endsWith("/oauth/introspect"));
const mints = () => calls.filter((c) => c.url.endsWith("/internal/step-up-token"));
const flaskReads = () => calls.filter((c) => !c.url.endsWith("/oauth/introspect") && !c.url.endsWith("/internal/step-up-token"));

describe("the refusal chain (§8.2)", () => {
  test("R1: no credential is 401, readable from a browser, with a way forward", async () => {
    const res = await initialize();
    expect(res.status).toBe(401);
    expect(res.headers["www-authenticate"]).toContain("resource_metadata=");
    expect(res.headers["www-authenticate"]).not.toContain("error=");
    expect(res.headers["access-control-allow-origin"]).toBe("*");
    expect(res.headers["access-control-expose-headers"]).toContain("WWW-Authenticate");
    expect(introspections()).toHaveLength(0);
  });

  test("R2: a random string as bearer is 401 invalid_token", async () => {
    introspection = { active: false };
    const res = await initialize("Bearer garbage");
    expect(res.status).toBe(401);
    expect(res.headers["www-authenticate"]).toContain('error="invalid_token"');
    expect(res.body).toEqual({ error: "Unauthorized" });
  });

  test("R3: a token minted for the FHIR audience is refused here, 401 not 403", async () => {
    introspection = liveIntrospection({ aud: FHIR_RESOURCE });
    const res = await initialize(`Bearer ${OAUTH_TOKEN}`);
    expect(res.status).toBe(401);
    expect(introspections()).toHaveLength(1);
  });

  test.each([
    ["expired", { exp: Math.floor(Date.now() / 1000) - 5 }],
    ["no read scope", { scope: "fhir.write" }],
    ["no audience at all", { aud: undefined }],
    ["a near-miss audience", { aud: CANONICAL + "/" }],
    ["a tenant that is not a tenant id", { tenant_id: "../x; rm" }],
  ])("R4 and §8.3: %s is 401", async (_label, overrides) => {
    introspection = liveIntrospection(overrides);
    const res = await initialize(`Bearer ${OAUTH_TOKEN}`);
    expect(res.status).toBe(401);
  });

  test("R5: MCP_AUTH_TOKEN still opens the door and never touches Flask", async () => {
    const res = await initialize(`Bearer ${STATIC_TOKEN}`);
    expect(res.status).toBe(200);
    expect(introspections()).toHaveLength(0);
  });

  test("R7: MCP_OAUTH_ENABLED off refuses an otherwise valid token without asking Flask", async () => {
    delete process.env.MCP_OAUTH_ENABLED;
    const res = await initialize(`Bearer ${OAUTH_TOKEN}`);
    expect(res.status).toBe(401);
    expect(introspections()).toHaveLength(0);
  });

  test("introspection failing closed: Flask unreachable is 401, never a guess", async () => {
    introspection = () => {
      throw new Error("ECONNREFUSED");
    };
    const res = await initialize(`Bearer ${OAUTH_TOKEN}`);
    expect(res.status).toBe(401);
  });

  test("a preflight on the transport answers with an allow-origin so the 401 can be read", async () => {
    const res = await request(app).options("/mcp").set("Origin", "https://claude.ai").set("Host", CANONICAL_HOST);
    expect(res.status).toBe(204);
    expect(res.headers["access-control-allow-origin"]).toBe("*");
  });
});

describe("the positive chain on a consented tenant (§13.6)", () => {
  test("A7: a live token initializes and lists tools", async () => {
    const sessionId = await session();
    expect(sessionId).toBeTruthy();
    const res = await request(app)
      .post("/mcp")
      .set("Host", CANONICAL_HOST)
      .set("Authorization", `Bearer ${OAUTH_TOKEN}`)
      .set("Mcp-Session-Id", sessionId)
      .send({ jsonrpc: "2.0", id: 3, method: "tools/list" });
    expect(res.status).toBe(200);
    expect(res.body.result.tools.length).toBeGreaterThan(20);
  });

  test("a read reaches Flask as the introspected tenant with a read-scoped token this server minted", async () => {
    const sessionId = await session();
    const res = await call(sessionId, "fhir_read", { resource_type: "Patient", resource_id: "p-1" });
    expect(res.status).toBe(200);
    expect(res.body.result.isError).toBeUndefined();

    expect(mints()).toHaveLength(1);
    const mint = mints()[0];
    expect(JSON.parse(mint.init.body)).toEqual({ tenant_id: TENANT, scope: "read" });
    expect(mint.init.headers["X-Internal-Secret"]).toBe("mint-secret");

    expect(flaskReads()).toHaveLength(1);
    const headers = flaskReads()[0].init.headers as Record<string, string>;
    expect(headers["X-Tenant-Id"]).toBe(TENANT);
    expect(headers["X-Step-Up-Token"]).toBe(`read-token-for-${TENANT}`);
    expect(headers["Authorization"]).toBeUndefined();
    expect(headers["X-Agent-Id"]).toBe("oauth:cid-claude");
  });

  test("R8: credentials in tool arguments reach Flask in no form on the OAuth path", async () => {
    const sessionId = await session();
    const res = await call(sessionId, "fhir_read", {
      resource_type: "Patient",
      resource_id: "p-1",
      _tenantId: "victim-tenant",
      _stepUpToken: "forged-step-up",
      _authorization: "Bearer forged-bearer",
      _fhirServerUrl: "https://evil.example/fhir",
      _fhirAccessToken: "upstream-token",
      _patientId: "someone-else",
    });
    expect(res.status).toBe(200);
    const flask = flaskReads();
    expect(flask).toHaveLength(1);
    const headers = flask[0].init.headers as Record<string, string>;
    expect(headers["X-Tenant-Id"]).toBe(TENANT);
    expect(headers["X-Step-Up-Token"]).toBe(`read-token-for-${TENANT}`);
    expect(headers["Authorization"]).toBeUndefined();
    expect(headers["X-FHIR-Server-URL"]).toBeUndefined();
    expect(headers["X-FHIR-Access-Token"]).toBeUndefined();
    expect(headers["X-Patient-ID"]).toBeUndefined();
    const everything = JSON.stringify(calls);
    for (const leaked of ["victim-tenant", "forged-step-up", "forged-bearer", "evil.example", "upstream-token", "someone-else"]) {
      expect(everything).not.toContain(leaked);
    }
  });

  test("the inbound Authorization header is not forwarded either (§3.6)", async () => {
    const sessionId = await session();
    await call(sessionId, "fhir_read", { resource_type: "Patient", resource_id: "p-1" });
    const everything = JSON.stringify(flaskReads());
    expect(everything).not.toContain(OAUTH_TOKEN);
  });

  test("a write-tier tool is refused before anything is sent", async () => {
    const sessionId = await session();
    const res = await call(sessionId, "fhir_commit_write", {
      resource: { resourceType: "Observation", status: "final" },
      operation: "create",
      _stepUpToken: "a-real-looking-step-up-token",
    });
    expect(res.status).toBe(200);
    expect(res.body.result.isError).toBe(true);
    expect(res.body.result.content[0].text).toContain("read-only");
    expect(mints()).toHaveLength(0);
    expect(flaskReads()).toHaveLength(0);
  });

  test("the mint refusing, or answering without the read scope, is a refusal here, not a wider call", async () => {
    const sessionId = await session();
    mockFetch.mockImplementation(async (url: string, init: Record<string, any> = {}) => {
      calls.push({ url, init });
      if (url.endsWith("/oauth/introspect")) return respond(200, liveIntrospection());
      if (url.endsWith("/internal/step-up-token")) return respond(200, { token: "full-token", tenant_id: TENANT, scope: null });
      return respond(200, { resourceType: "Patient" });
    });
    const res = await call(sessionId, "fhir_read", { resource_type: "Patient", resource_id: "p-1" });
    expect(res.body.result.isError).toBe(true);
    expect(res.body.result.content[0].text).toContain("read_credential_unavailable");
    expect(flaskReads()).toHaveLength(0);
  });

  test("introspection and the mint are cached under the token, once per token", async () => {
    const sessionId = await session();
    await call(sessionId, "fhir_read", { resource_type: "Patient", resource_id: "p-1" });
    await call(sessionId, "fhir_read", { resource_type: "Patient", resource_id: "p-2" });
    expect(introspections()).toHaveLength(1);
    expect(mints()).toHaveLength(1);
    expect(flaskReads()).toHaveLength(2);
    expect(JSON.stringify(calls)).not.toContain(OAUTH_TOKEN.slice(0, 12) + '"');
  });
});

describe("R8 on the SSE transport, where the overrides live", () => {
  const overrides = {
    _tenantId: "victim-tenant",
    _stepUpToken: "forged-step-up",
    _authorization: "Bearer forged-bearer",
    _fhirServerUrl: "https://evil.example/fhir",
    _fhirAccessToken: "upstream-token",
    _patientId: "someone-else",
  };

  test("without a grant the overrides apply, as they always have", () => {
    const args: Record<string, unknown> = { resource_type: "Patient", ...overrides };
    const headers = applyToolArgumentOverrides(args, { "x-tenant-id": "session-tenant" });
    expect(headers["x-tenant-id"]).toBe("victim-tenant");
    expect(headers["x-step-up-token"]).toBe("forged-step-up");
    expect(headers["authorization"]).toBe("Bearer forged-bearer");
    expect(headers["x-fhir-server-url"]).toBe("https://evil.example/fhir");
    expect(args).toEqual({ resource_type: "Patient" });
  });

  test("with a grant every override is removed and none is applied", () => {
    const session = {
      "x-tenant-id": TENANT,
      "x-agent-id": "oauth:cid-claude",
      "x-oauth-grant": JSON.stringify({ tenantId: TENANT, tokenHash: "h", expiresAtMs: 1 }),
    };
    const args: Record<string, unknown> = { resource_type: "Patient", ...overrides };
    const headers = applyToolArgumentOverrides(args, session);
    expect(headers).toEqual(session);
    expect(args).toEqual({ resource_type: "Patient" });
  });
});

describe("an SSE session is bound to the credential that opened it", () => {
  test("the binding is the static token or one OAuth token's hash", () => {
    expect(sessionCredential(undefined)).toBe("static");
    const grant = { tenantId: TENANT, clientId: "c", scopes: ["fhir.read"], tokenHash: "abc", expiresAtMs: 1 };
    expect(sessionCredential(grant)).toBe("oauth:abc");
    expect(sessionCredential({ ...grant, tokenHash: "def" })).not.toBe(sessionCredential(grant));
  });

  test("a POST to /messages under another token is refused; the same token is accepted", async () => {
    const server = http.createServer(app);
    await new Promise<void>((resolve) => server.listen(0, resolve));
    const port = (server.address() as AddressInfo).port;
    // The stream stays open while the POSTs below run: closing it would
    // delete the session, and a 400 for a vanished session is not the
    // refusal under test.
    let stream: http.ClientRequest | undefined;
    try {
      // Open the stream with the OAuth token and read the endpoint event.
      const sessionId = await new Promise<string>((resolve, reject) => {
        stream = http.request(
          { port, path: "/sse", method: "GET", headers: { Host: CANONICAL_HOST, Authorization: `Bearer ${OAUTH_TOKEN}` } },
          (res) => {
            if (res.statusCode !== 200) return reject(new Error(`sse ${res.statusCode}`));
            let buf = "";
            res.on("data", (chunk) => {
              buf += chunk.toString();
              const m = /sessionId=([A-Za-z0-9-]+)/.exec(buf);
              if (m) resolve(m[1]);
            });
          }
        );
        stream.on("error", () => { /* destroyed at the end */ });
        stream.end();
      });

      const post = (auth: string) =>
        new Promise<number>((resolve, reject) => {
          const req = http.request(
            { port, path: `/messages?sessionId=${sessionId}`, method: "POST",
              headers: { Host: CANONICAL_HOST, "Content-Type": "application/json", Authorization: auth } },
            (res) => { res.resume(); res.on("end", () => resolve(res.statusCode || 0)); }
          );
          req.on("error", reject);
          req.end(JSON.stringify({ jsonrpc: "2.0", id: 9, method: "ping" }));
        });

      // Another token, for another tenant, that introspection also accepts.
      introspection = liveIntrospection({ tenant_id: "some-other-tenant" });
      expect(await post("Bearer another-valid-token")).toBe(401);
      // The static credential is not this session's credential either.
      expect(await post(`Bearer ${STATIC_TOKEN}`)).toBe(401);
      // The token that opened it is accepted (the transport may already be
      // closed by then, which is any status but a refusal).
      introspection = liveIntrospection();
      expect(await post(`Bearer ${OAUTH_TOKEN}`)).not.toBe(401);
    } finally {
      stream?.destroy();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });
});

describe("boot", () => {
  test("R6 still holds: production without the static token and without demo refuses to start", () => {
    const { assertMCPAuthConfigured } = require("./index");
    expect(() => assertMCPAuthConfigured({ NODE_ENV: "production" })).toThrow(/MCP_AUTH_TOKEN/);
  });

  test("enabled without every dependency refuses to start; unset is fine", () => {
    expect(() => assertMCPOAuthConfigured({})).not.toThrow();
    expect(() =>
      assertMCPOAuthConfigured({ MCP_OAUTH_ENABLED: "true", MCP_CANONICAL_RESOURCE: CANONICAL })
    ).toThrow(/MCP_INTROSPECTION_CLIENT_ID/);
    expect(() =>
      assertMCPOAuthConfigured({
        MCP_OAUTH_ENABLED: "true",
        MCP_CANONICAL_RESOURCE: "https://MCP.healthclaw.io/mcp",
        MCP_INTROSPECTION_CLIENT_ID: "a",
        MCP_INTROSPECTION_CLIENT_SECRET: "b",
        INTERNAL_TOKEN_MINT_SECRET: "c",
      })
    ).toThrow(/canonical/);
    expect(() =>
      assertMCPOAuthConfigured({
        MCP_OAUTH_ENABLED: "true",
        MCP_CANONICAL_RESOURCE: CANONICAL,
        MCP_INTROSPECTION_CLIENT_ID: "a",
        MCP_INTROSPECTION_CLIENT_SECRET: "b",
        INTERNAL_TOKEN_MINT_SECRET: "c",
      })
    ).not.toThrow();
  });
});
