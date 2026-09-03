/**
 * Phase 1 of MCP authorization: the refusal says where to go, and nothing else
 * changes.
 *
 * `docs/specs/2026-08-16-mcp-authorization.md` §3.2/§3.4, amended 2026-09-02
 * (P1-a, P1-b). Today an unauthenticated POST /mcp answers `WWW-Authenticate:
 * Bearer` and every well-known path 404s, so a conformant client has nowhere
 * to go and asks the user for a Client ID that does not exist (#290, #523).
 *
 * Two properties this suite exists to pin, both of which are silent when
 * broken:
 *
 *   1. Unset `MCP_CANONICAL_RESOURCE` is today's behaviour exactly — 404 and a
 *      bare `Bearer`. The flag ships dark, so the "no change" half is the half
 *      that has to be asserted.
 *   2. The `resource_metadata` URL and the PRM `resource` are built from the
 *      pinned constant and never from the request Host. A proxy that rewrites
 *      Host would otherwise mint a second identity for this server, and the
 *      document would be confidently wrong rather than absent — the worse
 *      failure, per the spec's §9.3.
 *
 * Who gets in does not change anywhere in this file: the static-token path
 * still admits exactly `MCP_AUTH_TOKEN` and nothing else.
 */

import request from "supertest";
import {
  app,
  assertMCPCanonicalResourceConfigured,
  closeMCPServerForTests,
} from "./index";

const CANONICAL = "https://mcp.healthclaw.io/mcp";
const CANONICAL_HOST = "mcp.healthclaw.io";
const RAILWAY_HOST = "mcp-server-production-5112.up.railway.app";
const PRM_SUBPATH = "/.well-known/oauth-protected-resource/mcp";
const PRM_ROOT = "/.well-known/oauth-protected-resource";
const STATIC_TOKEN = "static-mcp-token-for-tests";

const savedEnv: Record<string, string | undefined> = {};

beforeEach(() => {
  savedEnv.MCP_CANONICAL_RESOURCE = process.env.MCP_CANONICAL_RESOURCE;
  savedEnv.MCP_AUTH_TOKEN = process.env.MCP_AUTH_TOKEN;
  savedEnv.MCP_PUBLIC_DEMO = process.env.MCP_PUBLIC_DEMO;
  delete process.env.MCP_CANONICAL_RESOURCE;
  delete process.env.MCP_PUBLIC_DEMO;
  process.env.MCP_AUTH_TOKEN = STATIC_TOKEN;
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

function initialize(host: string) {
  return request(app)
    .post("/mcp")
    .set("Host", host)
    .send({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "t", version: "1" } },
    });
}

describe("with MCP_CANONICAL_RESOURCE unset, nothing changes", () => {
  it("serves no protected-resource metadata at either path", async () => {
    for (const path of [PRM_SUBPATH, PRM_ROOT]) {
      const res = await request(app).get(path).set("Host", CANONICAL_HOST);
      expect({ path, status: res.status }).toEqual({ path, status: 404 });
    }
  });

  it("challenges with a bare Bearer, as it does today", async () => {
    const res = await initialize(CANONICAL_HOST);
    expect(res.status).toBe(401);
    expect(res.headers["www-authenticate"]).toBe("Bearer");
    expect(res.body).toEqual({ error: "Unauthorized" });
  });

  it("challenges with a bare Bearer when a credential was presented too", async () => {
    const res = await initialize(CANONICAL_HOST).set("Authorization", "Bearer not-the-token");
    expect(res.status).toBe(401);
    expect(res.headers["www-authenticate"]).toBe("Bearer");
  });
});

describe("with the constant set, the canonical host serves the metadata", () => {
  beforeEach(() => {
    process.env.MCP_CANONICAL_RESOURCE = CANONICAL;
  });

  it("returns the RFC 9728 document at the header-pointed sub-path", async () => {
    const res = await request(app).get(PRM_SUBPATH).set("Host", CANONICAL_HOST);
    expect(res.status).toBe(200);
    expect(res.headers["content-type"]).toMatch(/application\/json/);
    expect(res.body).toEqual({
      resource: CANONICAL,
      authorization_servers: ["https://app.healthclaw.io"],
      scopes_supported: ["fhir.read", "context.read"],
      bearer_methods_supported: ["header"],
      resource_name: "HealthClaw Guardrails",
      resource_documentation: "https://app.healthclaw.io/r6/fhir/docs/privacy-policy",
    });
  });

  it("returns the same document at the root fallback path", async () => {
    const subPath = await request(app).get(PRM_SUBPATH).set("Host", CANONICAL_HOST);
    const root = await request(app).get(PRM_ROOT).set("Host", CANONICAL_HOST);
    expect(root.status).toBe(200);
    expect(root.body).toEqual(subPath.body);
  });

  it("serves the document to a caller with no credential, with the lock configured", async () => {
    // The PRM sits outside the lock on purpose: a document a client must read
    // before it can authenticate cannot itself require authentication.
    const res = await request(app).get(PRM_SUBPATH).set("Host", CANONICAL_HOST);
    expect(res.status).toBe(200);
    expect(process.env.MCP_AUTH_TOKEN).toBe(STATIC_TOKEN);
  });

  it("carries the CORS header a browser-context client needs to read it", async () => {
    const res = await request(app)
      .get(PRM_SUBPATH)
      .set("Host", CANONICAL_HOST)
      .set("Origin", "https://claude.ai");
    expect(res.headers["access-control-allow-origin"]).toBe("*");
  });

  it("does not answer at a well-known sub-path the constant does not name", async () => {
    const res = await request(app)
      .get("/.well-known/oauth-protected-resource/something-else")
      .set("Host", CANONICAL_HOST);
    expect(res.status).toBe(404);
  });

  it("names the metadata URL and the scopes in the challenge, with no error", async () => {
    const res = await initialize(CANONICAL_HOST);
    expect(res.status).toBe(401);
    const challenge = res.headers["www-authenticate"];
    expect(challenge).toContain(
      `resource_metadata="https://mcp.healthclaw.io/.well-known/oauth-protected-resource/mcp"`
    );
    expect(challenge).toContain(`scope="fhir.read context.read"`);
    // RFC 6750 §3.1: SHOULD NOT send `error` when no credential was presented.
    // There is no token to call invalid, and a client reads one as a rejection
    // of something it never sent.
    expect(challenge).not.toContain("error=");
    expect(res.body).toEqual({ error: "Unauthorized" });
  });

  it("says invalid_token only when a credential was presented and rejected", async () => {
    const res = await initialize(CANONICAL_HOST).set("Authorization", "Bearer not-the-token");
    expect(res.status).toBe(401);
    const challenge = res.headers["www-authenticate"];
    expect(challenge).toContain(`error="invalid_token"`);
    expect(challenge).toContain(
      `resource_metadata="https://mcp.healthclaw.io/.well-known/oauth-protected-resource/mcp"`
    );
    // The description distinguishes nothing: expired, wrong audience and not a
    // token at all recover identically, and telling them apart is an oracle.
    expect(challenge).not.toContain("not-the-token");
  });

  it("builds both the document and the header from the constant, not the Host", async () => {
    // MUTATION: build the URL from req.hostname instead and this goes red.
    // Not because a proxy is known to rewrite Host — Railway preserves it,
    // measured 2026-09-03. Because RFC 9728 §3.3 has a client reject a
    // document whose `resource` is not the identifier it inserted into the
    // well-known path, so what we advertise cannot follow the request.
    const spoofed = `${CANONICAL_HOST.toUpperCase()}:8443`;
    const prm = await request(app).get(PRM_SUBPATH).set("Host", spoofed);
    expect(prm.status).toBe(200);
    expect(prm.body.resource).toBe(CANONICAL);

    const challenge = (await initialize(spoofed)).headers["www-authenticate"];
    expect(challenge).toContain(
      `resource_metadata="https://mcp.healthclaw.io/.well-known/oauth-protected-resource/mcp"`
    );
    expect(challenge).not.toContain("8443");
  });
});

describe("with the constant set, a non-canonical host is unchanged", () => {
  beforeEach(() => {
    process.env.MCP_CANONICAL_RESOURCE = CANONICAL;
  });

  it("serves no metadata on the platform hostname", async () => {
    // RFC 9728 §3.3: a client rejects a document whose `resource` is not the
    // identifier it inserted into the well-known path. Serving this one on the
    // Railway hostname would hand that client a rejection instead of a 404.
    for (const path of [PRM_SUBPATH, PRM_ROOT]) {
      const res = await request(app).get(path).set("Host", RAILWAY_HOST);
      expect({ path, status: res.status }).toEqual({ path, status: 404 });
    }
  });

  it("keeps the bare Bearer challenge on the platform hostname", async () => {
    const res = await initialize(RAILWAY_HOST);
    expect(res.status).toBe(401);
    expect(res.headers["www-authenticate"]).toBe("Bearer");
  });
});

describe("who gets in does not change", () => {
  it.each([
    ["unset", undefined],
    ["set", CANONICAL],
  ])("admits the static token on both hosts with the constant %s", async (_label, value) => {
    if (value === undefined) delete process.env.MCP_CANONICAL_RESOURCE;
    else process.env.MCP_CANONICAL_RESOURCE = value;

    for (const host of [CANONICAL_HOST, RAILWAY_HOST]) {
      const res = await request(app)
        .post("/mcp")
        .set("Host", host)
        .set("Authorization", `Bearer ${STATIC_TOKEN}`)
        .send({ jsonrpc: "2.0", id: 2, method: "tools/list" });
      expect({ host, status: res.status }).toEqual({ host, status: 200 });
      expect(Array.isArray(res.body?.result?.tools)).toBe(true);
    }
  });

  it.each([
    ["unset", undefined],
    ["set", CANONICAL],
  ])("keeps /health public on both hosts with the constant %s", async (_label, value) => {
    if (value === undefined) delete process.env.MCP_CANONICAL_RESOURCE;
    else process.env.MCP_CANONICAL_RESOURCE = value;

    for (const host of [CANONICAL_HOST, RAILWAY_HOST]) {
      const res = await request(app).get("/health").set("Host", host);
      expect({ host, status: res.status }).toEqual({ host, status: 200 });
      expect(res.body.status).toBe("healthy");
    }
  });

  it("leaves the pinned-tenant demo endpoint unauthenticated", async () => {
    // The demo deployment runs without a credential by design. Phase 1 must
    // not put a challenge in front of it, and must not add a lock it never had.
    process.env.MCP_CANONICAL_RESOURCE = CANONICAL;
    process.env.MCP_PUBLIC_DEMO = "true";

    const res = await initialize(CANONICAL_HOST);
    expect(res.status).toBe(200);
    expect(res.headers["www-authenticate"]).toBeUndefined();
  });
});

describe("a malformed constant refuses to boot", () => {
  // Fail at start, not at the first client: a value that cannot be an audience
  // would otherwise disable phase 1 silently and look like it shipped.
  it.each([
    ["http, not https", "http://mcp.healthclaw.io/mcp"],
    ["not absolute", "mcp.healthclaw.io/mcp"],
    ["not a URL", "not a url at all"],
  ])("rejects a value that is %s", (_label, value) => {
    expect(() =>
      assertMCPCanonicalResourceConfigured({ MCP_CANONICAL_RESOURCE: value })
    ).toThrow(/MCP_CANONICAL_RESOURCE/);
  });

  it.each([
    ["unset", {}],
    ["empty", { MCP_CANONICAL_RESOURCE: "" }],
    ["the canonical value", { MCP_CANONICAL_RESOURCE: CANONICAL }],
  ])("starts with the constant %s", (_label, env) => {
    expect(() => assertMCPCanonicalResourceConfigured(env)).not.toThrow();
  });
});
