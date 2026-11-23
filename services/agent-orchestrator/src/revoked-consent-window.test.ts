/**
 * Security review of the closed-tenant refusal (#1157): how long a consented
 * connector keeps reading after its consent is revoked.
 *
 * Flask is a mock. Introspection answers live, then (after the revoke) not
 * live. The MCP server keeps serving the remembered grant, and its minted
 * read credential, until INTROSPECTION_CACHE_TTL_MS runs out. These tests
 * pin that bound: served at +299 s, refused at +301 s. They pass today and
 * document the window. They turn red if the bound grows, and they need a
 * new number if it shrinks.
 */
import http from "http";
import request from "supertest";

jest.mock("node-fetch", () => jest.fn());
import fetch from "node-fetch";
import {
  app,
  closeMCPServerForTests,
  resetOAuthStateForTests,
  resetRateLimitForTests,
} from "./index";
import { resetGrantReadTokenCacheForTests } from "./tools";

const mockFetch = fetch as unknown as jest.Mock;
const CANONICAL = "https://mcp.healthclaw.io/mcp";
const HOST = "mcp.healthclaw.io";
const TOKEN = "a-consented-connector-token";
const TENANT = "closed-window-t1";

let live = true;
let reads = 0;
let t0 = Date.now();
let nowSpy: jest.SpyInstance;

function respond(status: number, body: unknown) {
  return { ok: status < 300, status, json: async () => body,
           text: async () => JSON.stringify(body), headers: { get: () => null } };
}

beforeEach(() => {
  process.env.MCP_AUTH_TOKEN = "static-token";
  process.env.MCP_CANONICAL_RESOURCE = CANONICAL;
  process.env.MCP_OAUTH_ENABLED = "true";
  process.env.MCP_INTROSPECTION_CLIENT_ID = "mcp-server";
  process.env.MCP_INTROSPECTION_CLIENT_SECRET = "introspection-secret";
  process.env.INTERNAL_TOKEN_MINT_SECRET = "mint-secret";
  delete process.env.MCP_PUBLIC_DEMO;
  live = true;
  reads = 0;
  t0 = Date.now();
  nowSpy = jest.spyOn(Date, "now").mockImplementation(() => t0);
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string, init: Record<string, any> = {}) => {
    if (url.endsWith("/oauth/introspect")) {
      return respond(200, live ? {
        active: true, token_type: "Bearer", aud: CANONICAL, scope: "fhir.read",
        tenant_id: TENANT, client_id: "cid-x", exp: Math.floor(t0 / 1000) + 3600,
      } : { active: false });
    }
    if (url.endsWith("/internal/step-up-token")) {
      const body = JSON.parse(init.body || "{}");
      return respond(200, { token: "read-token", tenant_id: body.tenant_id, scope: "read" });
    }
    reads += 1;
    return respond(200, { resourceType: "Patient", id: "p-1" });
  });
  resetOAuthStateForTests();
  resetRateLimitForTests();
  resetGrantReadTokenCacheForTests();
});

afterEach(() => nowSpy.mockRestore());

let server: http.Server;
beforeAll(async () => {
  server = http.createServer(app);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
});
afterAll(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
  closeMCPServerForTests();
});

async function readOnce(): Promise<number> {
  const init = await request(server).post("/mcp").set("Host", HOST)
    .set("Authorization", `Bearer ${TOKEN}`)
    .send({ jsonrpc: "2.0", id: 1, method: "initialize", params: {
      protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "t", version: "0" } } });
  if (init.status !== 200) return init.status;
  const res = await request(server).post("/mcp").set("Host", HOST)
    .set("Authorization", `Bearer ${TOKEN}`)
    .set("Mcp-Session-Id", init.headers["mcp-session-id"])
    .send({ jsonrpc: "2.0", id: 2, method: "tools/call",
            params: { name: "fhir_read", arguments: { resource_type: "Patient", resource_id: "p-1" } } });
  return res.status;
}

test("a revoked consent is still served for just under five minutes", async () => {
  expect(await readOnce()).toBe(200);
  expect(reads).toBe(1);

  live = false; // the consent is revoked at the engine

  nowSpy.mockImplementation(() => t0 + 299 * 1000);
  expect(await readOnce()).toBe(200);
  expect(reads).toBe(2); // the closed tenant was read after the revoke

  nowSpy.mockImplementation(() => t0 + 301 * 1000);
  expect(await readOnce()).toBe(401);
  expect(reads).toBe(2);
});
