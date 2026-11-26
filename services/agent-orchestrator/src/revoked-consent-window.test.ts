/**
 * How long a consented connector keeps reading through this server after
 * the engine stops honouring its grant: the consent was revoked, or the
 * tenant was closed. Both reach this server the same way, as introspection
 * answering inactive, so one test covers both.
 *
 * The window is a deliberate limit (#1183). A remembered "yes", and the read
 * credential minted under it, is served until INTROSPECTION_CACHE_TTL_MS
 * runs out, and nothing invalidates it early. Two things are pinned:
 *
 * - The behaviour, read from the constant: served one second before the TTL
 *   ends, refused one second after. This follows the constant, so it stays
 *   green when the constant changes and red when the cache stops honouring
 *   it.
 * - The ceiling, stated here once: the TTL is at most the five minutes the
 *   description of the limit promises. Raising the constant past that turns
 *   this red; lowering it does not.
 *
 * The engine is a mock (node-fetch is faked). This proves what this server's
 * cache does with an inactive answer. It proves nothing about the engine's
 * own refusal, which tests/test_closed_tenant_connector_exploits.py covers.
 */
import http from "http";
import request from "supertest";

jest.mock("node-fetch", () => jest.fn());
import fetch from "node-fetch";
import {
  app,
  INTROSPECTION_CACHE_TTL_MS,
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
//: The longest window the limit is documented to allow (#1183).
const DOCUMENTED_CEILING_MS = 5 * 60 * 1000;

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

test("the window is no longer than the documented five minutes", () => {
  expect(INTROSPECTION_CACHE_TTL_MS).toBeGreaterThan(0);
  expect(INTROSPECTION_CACHE_TTL_MS).toBeLessThanOrEqual(DOCUMENTED_CEILING_MS);
});

test("a grant the engine stopped honouring is served until the cache TTL ends, then refused", async () => {
  expect(await readOnce()).toBe(200);
  expect(reads).toBe(1);

  live = false; // the engine now answers inactive: consent revoked, or tenant closed

  nowSpy.mockImplementation(() => t0 + INTROSPECTION_CACHE_TTL_MS - 1000);
  expect(await readOnce()).toBe(200);
  expect(reads).toBe(2); // read after the engine said no: the window

  nowSpy.mockImplementation(() => t0 + INTROSPECTION_CACHE_TTL_MS + 1000);
  expect(await readOnce()).toBe(401);
  expect(reads).toBe(2);
});
