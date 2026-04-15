/**
 * Tests for FHIR R6 MCP Server -- tool schemas, tool execution, and Express endpoints.
 *
 * Three describe blocks:
 * 1. Tool Schema Tests -- pure unit tests, no network.
 * 2. Tool Execution Tests -- FHIRTools.executeTool with mocked node-fetch.
 * 3. Express App Tests -- supertest against the exported Express app.
 */

import { FHIRTools, MCPToolSchema } from "./tools";

// Mock node-fetch before importing anything that uses it.
// jest.mock is hoisted, so the factory must not reference outer variables.
jest.mock("node-fetch", () => jest.fn());
import fetch from "node-fetch";
const mockFetch = fetch as unknown as jest.Mock;

import request from "supertest";
import { app } from "./index";

// The index module starts a setInterval for session cleanup. Use fake timers
// so Jest can exit cleanly without --forceExit.
beforeAll(() => {
  jest.useFakeTimers();
});
afterAll(() => {
  jest.useRealTimers();
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Build a minimal node-fetch Response-like object. */
function fakeResponse(body: Record<string, unknown>, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: jest.fn().mockResolvedValue(body),
  };
}

const EXPECTED_TOOL_NAMES = [
  "context_get",
  "curatr_apply_fix",
  "curatr_evaluate",
  "fhir_commit_write",
  "fhir_compiled_truth",
  "fhir_get_token",
  "fhir_lastn",
  "fhir_permission_evaluate",
  "fhir_propose_write",
  "fhir_read",
  "fhir_search",
  "fhir_seed",
  "fhir_stats",
  "fhir_subscription_topics",
  "fhir_validate",
  "wearables_sync_status",
];

const EXPECTED_TOOL_NAME_SET = new Set(EXPECTED_TOOL_NAMES);

const READ_ONLY_TOOL_NAMES = [
  "context_get",
  "fhir_read",
  "fhir_search",
  "fhir_validate",
  "fhir_stats",
  "fhir_lastn",
  "fhir_permission_evaluate",
  "fhir_subscription_topics",
  "fhir_compiled_truth",
  "wearables_sync_status",
];

// ---------------------------------------------------------------------------
// 1. Tool Schema Tests (no network needed)
// ---------------------------------------------------------------------------

describe("Tool Schema Tests", () => {
  const tools = new FHIRTools("http://localhost:5000/r6/fhir");
  let schemas: MCPToolSchema[];

  beforeAll(() => {
    schemas = tools.getMCPToolSchemas();
  });

  it("getMCPToolSchemas() returns exactly 16 tools", () => {
    expect(schemas).toHaveLength(16);
  });

  it("every tool has required MCP fields: name, description, inputSchema, annotations", () => {
    for (const tool of schemas) {
      expect(tool).toHaveProperty("name");
      expect(tool).toHaveProperty("description");
      expect(tool).toHaveProperty("inputSchema");
      expect(tool).toHaveProperty("annotations");

      expect(typeof tool.name).toBe("string");
      expect(tool.name.length).toBeGreaterThan(0);
      expect(typeof tool.description).toBe("string");
      expect(tool.description.length).toBeGreaterThan(0);
      expect(typeof tool.inputSchema).toBe("object");
      expect(typeof tool.annotations).toBe("object");
    }
  });

  it("all 16 tool names match the expected set", () => {
    const actualNames = schemas.map((t) => t.name).sort();
    expect(actualNames).toEqual(EXPECTED_TOOL_NAMES);
  });

  it("annotations include readOnlyHint, destructiveHint, and openWorldHint booleans", () => {
    for (const tool of schemas) {
      expect(typeof tool.annotations.readOnlyHint).toBe("boolean");
      expect(typeof tool.annotations.destructiveHint).toBe("boolean");
      expect(typeof tool.annotations.openWorldHint).toBe("boolean");
    }
  });

  it("read tools have readOnlyHint: true and destructiveHint: false", () => {
    for (const name of READ_ONLY_TOOL_NAMES) {
      const tool = schemas.find((t) => t.name === name)!;
      expect(tool).toBeDefined();
      expect(tool.annotations.readOnlyHint).toBe(true);
      expect(tool.annotations.destructiveHint).toBe(false);
    }
  });

  it("fhir_propose_write has readOnlyHint: true (preview only, no side effects)", () => {
    const propose = schemas.find((t) => t.name === "fhir_propose_write")!;
    expect(propose.annotations.readOnlyHint).toBe(true);
    expect(propose.annotations.destructiveHint).toBe(false);
  });

  it("fhir_commit_write has destructiveHint: true and readOnlyHint: false", () => {
    const commit = schemas.find((t) => t.name === "fhir_commit_write")!;
    expect(commit.annotations.readOnlyHint).toBe(false);
    expect(commit.annotations.destructiveHint).toBe(true);
  });

  it("every inputSchema has type: object", () => {
    for (const tool of schemas) {
      expect(tool.inputSchema.type).toBe("object");
    }
  });

  it("schemas do not expose the internal tier field", () => {
    for (const tool of schemas) {
      expect((tool as unknown as Record<string, unknown>).tier).toBeUndefined();
    }
  });
});

// ---------------------------------------------------------------------------
// 2. Tool Execution Tests (mocked node-fetch)
// ---------------------------------------------------------------------------

describe("Tool Execution Tests", () => {
  const BASE = "http://localhost:5000/r6/fhir";
  const tools = new FHIRTools(BASE);

  afterEach(() => {
    mockFetch.mockReset();
  });

  // -- fhir.read --

  it("fhir.read proxies to correct URL with resource type and ID", async () => {
    const fhirPatient = { resourceType: "Patient", id: "pt-1", name: [{ family: "Test" }] };
    mockFetch.mockResolvedValueOnce(fakeResponse(fhirPatient));

    const result = await tools.executeTool("fhir_read", {
      resource_type: "Patient",
      resource_id: "pt-1",
    });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toBe(`${BASE}/Patient/pt-1`);
    expect(opts.headers["Content-Type"]).toBe("application/fhir+json");
    expect(result).toEqual(fhirPatient);
  });

  it("fhir.read returns error object when upstream returns non-OK status", async () => {
    mockFetch.mockResolvedValueOnce(fakeResponse({}, 404));

    const result = await tools.executeTool("fhir_read", {
      resource_type: "Patient",
      resource_id: "nonexistent",
    });

    expect(result).toHaveProperty("error");
    expect((result as Record<string, unknown>).error).toContain("404");
  });

  // -- fhir.search --

  it("fhir.search builds correct query params and adds _mcp_summary", async () => {
    const bundle = {
      resourceType: "Bundle",
      type: "searchset",
      total: 3,
      entry: [{ resource: { resourceType: "Observation" } }],
    };
    mockFetch.mockResolvedValueOnce(fakeResponse(bundle));

    const result = await tools.executeTool("fhir_search", {
      resource_type: "Observation",
      patient: "Patient/pt-1",
      code: "2339-0",
      status: "final",
      _count: 10,
      _sort: "-_lastUpdated",
    });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url] = mockFetch.mock.calls[0];
    expect(url).toContain(`${BASE}/Observation?`);
    expect(url).toContain("patient=Patient%2Fpt-1");
    expect(url).toContain("code=2339-0");
    expect(url).toContain("status=final");
    expect(url).toContain("_count=10");
    expect(url).toContain("_sort=-_lastUpdated");

    // Verify _mcp_summary is appended
    expect(result).toHaveProperty("_mcp_summary");
    const summary = (result as Record<string, unknown>)._mcp_summary as Record<string, unknown>;
    expect(summary.total).toBe(3);
    expect(summary.filters_applied).toEqual(
      expect.arrayContaining([
        expect.stringContaining("patient="),
        expect.stringContaining("code="),
        expect.stringContaining("status="),
      ])
    );
  });

  it("fhir.search caps _count at 50 (MAX_RESULT_ENTRIES)", async () => {
    const bundle = { resourceType: "Bundle", type: "searchset", total: 0, entry: [] };
    mockFetch.mockResolvedValueOnce(fakeResponse(bundle));

    await tools.executeTool("fhir_search", {
      resource_type: "Patient",
      _count: 999,
    });

    const [url] = mockFetch.mock.calls[0];
    expect(url).toContain("_count=50");
  });

  it("fhir.search adds note about zero results in _mcp_summary", async () => {
    const bundle = { resourceType: "Bundle", type: "searchset", total: 0, entry: [] };
    mockFetch.mockResolvedValueOnce(fakeResponse(bundle));

    const result = await tools.executeTool("fhir_search", {
      resource_type: "Observation",
      code: "nonexistent",
    });

    const summary = (result as Record<string, unknown>)._mcp_summary as Record<string, unknown>;
    expect(summary.total).toBe(0);
    expect(summary.note).toContain("No Observation resources found");
  });

  // -- fhir.commit_write (step-up enforcement) --

  it("fhir.commit_write requires step-up token (returns error without it)", async () => {
    const resource = { resourceType: "Observation", status: "final" };
    const result = await tools.executeTool(
      "fhir_commit_write",
      { resource, operation: "create" },
      {} // no step-up token
    );

    expect(result).toHaveProperty("error", "Step-up authorization required");
    expect(result).toHaveProperty("requires_step_up", true);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("fhir.commit_write returns step-up error when headers are undefined", async () => {
    const result = await tools.executeTool("fhir_commit_write", {
      resource: { resourceType: "Observation", status: "final" },
      operation: "create",
    });

    expect(result).toHaveProperty("error", "Step-up authorization required");
    expect(result).toHaveProperty("requires_step_up", true);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("fhir.commit_write proceeds with step-up token (create uses POST)", async () => {
    const resource = { resourceType: "Observation", status: "final" };
    const created = { ...resource, id: "obs-new" };
    mockFetch.mockResolvedValueOnce(fakeResponse(created));

    const result = await tools.executeTool(
      "fhir_commit_write",
      { resource, operation: "create" },
      { "x-step-up-token": "valid-token-123" }
    );

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toBe(`${BASE}/Observation`);
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-Step-Up-Token"]).toBe("valid-token-123");
    expect(result).toEqual(created);
  });

  it("fhir.commit_write with update uses PUT and includes resource ID in URL", async () => {
    const resource = { resourceType: "Patient", id: "pt-1", name: [{ family: "Updated" }] };
    mockFetch.mockResolvedValueOnce(fakeResponse(resource));

    await tools.executeTool(
      "fhir_commit_write",
      { resource, operation: "update" },
      { "x-step-up-token": "token-456" }
    );

    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toBe(`${BASE}/Patient/pt-1`);
    expect(opts.method).toBe("PUT");
  });

  it("fhir.commit_write with update returns error if resource has no ID", async () => {
    const resource = { resourceType: "Patient", name: [{ family: "NoId" }] };

    const result = await tools.executeTool(
      "fhir_commit_write",
      { resource, operation: "update" },
      { "x-step-up-token": "token-789" }
    );

    expect(result).toHaveProperty("error", "Resource ID required for update");
    expect(mockFetch).not.toHaveBeenCalled();
  });

  // -- fhir.validate --

  it("fhir.validate posts to $validate endpoint", async () => {
    const operationOutcome = {
      resourceType: "OperationOutcome",
      issue: [{ severity: "information", code: "informational", diagnostics: "OK" }],
    };
    mockFetch.mockResolvedValueOnce(fakeResponse(operationOutcome));

    const resource = { resourceType: "Patient", name: [{ family: "Test" }] };
    const result = await tools.executeTool("fhir_validate", { resource });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toBe(`${BASE}/Patient/$validate`);
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body)).toEqual(resource);
    expect(result).toEqual(operationOutcome);
  });

  // -- Unknown tool --

  it("unknown tool returns error", async () => {
    const result = await tools.executeTool("fhir_nonexistent", {});
    expect(result).toHaveProperty("error", "Unknown tool: fhir_nonexistent");
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("empty tool name returns error", async () => {
    const result = await tools.executeTool("", {});
    expect(result).toHaveProperty("error", "Unknown tool: ");
    expect(mockFetch).not.toHaveBeenCalled();
  });

  // -- fhir.stats --

  it("fhir.stats proxies to Observation/$stats with code and patient params", async () => {
    const statsResult = {
      resourceType: "Parameters",
      parameter: [
        { name: "count", valueInteger: 5 },
        { name: "mean", valueDecimal: 120.5 },
        { name: "unit", valueString: "mg/dL" },
      ],
    };
    mockFetch.mockResolvedValueOnce(fakeResponse(statsResult));

    const result = await tools.executeTool("fhir_stats", {
      code: "2339-0",
      patient: "Patient/pt-1",
    });

    const [url] = mockFetch.mock.calls[0];
    expect(url).toContain(`${BASE}/Observation/$stats`);
    expect(url).toContain("code=2339-0");
    expect(url).toContain("patient=Patient%2Fpt-1");
    expect(result).toHaveProperty("_mcp_summary");
    const summary = (result as Record<string, unknown>)._mcp_summary as Record<string, unknown>;
    expect(summary.observation_count).toBe(5);
  });

  // -- fhir.lastn --

  it("fhir.lastn proxies to Observation/$lastn with max param", async () => {
    const lastnResult = {
      resourceType: "Bundle",
      type: "searchset",
      total: 2,
      entry: [],
    };
    mockFetch.mockResolvedValueOnce(fakeResponse(lastnResult));

    const result = await tools.executeTool("fhir_lastn", {
      code: "8867-4",
      patient: "Patient/pt-1",
      max: 3,
    });

    const [url] = mockFetch.mock.calls[0];
    expect(url).toContain(`${BASE}/Observation/$lastn`);
    expect(url).toContain("max=3");
    expect(result).toHaveProperty("_mcp_summary");
    const summary = (result as Record<string, unknown>)._mcp_summary as Record<string, unknown>;
    expect(summary.max_requested).toBe(3);
  });

  // -- fhir.permission_evaluate --

  it("fhir.permission_evaluate posts to Permission/$evaluate", async () => {
    const evaluateResult = { decision: "permit", reasoning: "Practitioner has access" };
    mockFetch.mockResolvedValueOnce(fakeResponse(evaluateResult));

    const result = await tools.executeTool("fhir_permission_evaluate", {
      subject: "Practitioner/dr-1",
      action: "read",
      resource: "Patient/pt-1",
    });

    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toBe(`${BASE}/Permission/$evaluate`);
    expect(opts.method).toBe("POST");
    const sentBody = JSON.parse(opts.body);
    expect(sentBody.subject).toBe("Practitioner/dr-1");
    expect(sentBody.action).toBe("read");
    expect(result).toEqual(evaluateResult);
  });

  // -- fhir.subscription_topics --

  it("fhir.subscription_topics fetches SubscriptionTopic/$list", async () => {
    const topicList = {
      resourceType: "Bundle",
      type: "searchset",
      total: 1,
      entry: [{ resource: { resourceType: "SubscriptionTopic", id: "topic-1" } }],
    };
    mockFetch.mockResolvedValueOnce(fakeResponse(topicList));

    const result = await tools.executeTool("fhir_subscription_topics", {});

    const [url] = mockFetch.mock.calls[0];
    expect(url).toBe(`${BASE}/SubscriptionTopic/$list`);
    expect(result).toHaveProperty("_mcp_summary");
    const summary = (result as Record<string, unknown>)._mcp_summary as Record<string, unknown>;
    expect(summary.topic_count).toBe(1);
  });

  // -- context.get --

  it("context.get fetches the context envelope by ID", async () => {
    const envelope = { context_id: "ctx-123", patient: "Patient/pt-1", resources: [] };
    mockFetch.mockResolvedValueOnce(fakeResponse(envelope));

    const result = await tools.executeTool("context_get", { context_id: "ctx-123" });

    const [url] = mockFetch.mock.calls[0];
    expect(url).toBe(`${BASE}/context/ctx-123`);
    expect(result).toEqual(envelope);
  });

  // -- Header forwarding --

  it("forwards tenant, agent, and auth headers to upstream", async () => {
    mockFetch.mockResolvedValueOnce(fakeResponse({ resourceType: "Patient", id: "pt-1" }));

    await tools.executeTool(
      "fhir_read",
      { resource_type: "Patient", resource_id: "pt-1" },
      {
        "x-tenant-id": "tenant-abc",
        "x-agent-id": "agent-42",
        authorization: "Bearer tok123",
      }
    );

    const [, opts] = mockFetch.mock.calls[0];
    expect(opts.headers["X-Tenant-Id"]).toBe("tenant-abc");
    expect(opts.headers["X-Agent-Id"]).toBe("agent-42");
    expect(opts.headers["Authorization"]).toBe("Bearer tok123");
  });

  // -- X-Tenant-Id forwarding and fallback --

  it("X-Tenant-ID header is forwarded to Flask when present in the MCP request", async () => {
    mockFetch.mockResolvedValueOnce(
      fakeResponse({ resourceType: "Patient", id: "pt-1" })
    );

    await tools.executeTool(
      "fhir_read",
      { resource_type: "Patient", resource_id: "pt-1" },
      { "x-tenant-id": "test-tenant" }
    );

    const [, opts] = mockFetch.mock.calls[0];
    expect(opts.headers["X-Tenant-Id"]).toBe("test-tenant");
  });

  it("falls back to TENANT_ID env var when no X-Tenant-ID header is in the MCP request", async () => {
    const prev = process.env.TENANT_ID;
    process.env.TENANT_ID = "env-fallback-tenant";

    mockFetch.mockResolvedValueOnce(
      fakeResponse({ resourceType: "Patient", id: "pt-1" })
    );

    try {
      await tools.executeTool(
        "fhir_read",
        { resource_type: "Patient", resource_id: "pt-1" },
        {} // no x-tenant-id header
      );

      const [, opts] = mockFetch.mock.calls[0];
      expect(opts.headers["X-Tenant-Id"]).toBe("env-fallback-tenant");
    } finally {
      if (prev === undefined) delete process.env.TENANT_ID;
      else process.env.TENANT_ID = prev;
    }
  });

  it("falls back to desktop-demo when no X-Tenant-ID header and no TENANT_ID env var", async () => {
    const prev = process.env.TENANT_ID;
    delete process.env.TENANT_ID;

    mockFetch.mockResolvedValueOnce(
      fakeResponse({ resourceType: "Patient", id: "pt-1" })
    );

    try {
      await tools.executeTool(
        "fhir_read",
        { resource_type: "Patient", resource_id: "pt-1" }
        // headers argument omitted entirely
      );

      const [, opts] = mockFetch.mock.calls[0];
      expect(opts.headers["X-Tenant-Id"]).toBe("desktop-demo");
    } finally {
      if (prev !== undefined) process.env.TENANT_ID = prev;
    }
  });

  // -- propose_write does NOT require step-up --

  it("fhir.propose_write does NOT require step-up token", async () => {
    const validationResponse = {
      resourceType: "OperationOutcome",
      issue: [],
    };
    mockFetch.mockResolvedValueOnce(fakeResponse(validationResponse));

    const result = await tools.executeTool(
      "fhir_propose_write",
      {
        resource: { resourceType: "Observation", status: "final" },
        operation: "create",
      },
      {} // no step-up token -- should still work
    );

    expect(result.error).toBeUndefined();
    expect(result.proposal_status).toBe("ready");
  });
});

// ---------------------------------------------------------------------------
// 3. Express App Tests (supertest)
// ---------------------------------------------------------------------------

describe("Express App Tests", () => {
  afterEach(() => {
    mockFetch.mockReset();
  });

  // -- Health endpoint --

  describe("GET /health", () => {
    it("returns healthy status with version 1.0.0", async () => {
      const res = await request(app).get("/health");
      expect(res.status).toBe(200);
      expect(res.body.status).toBe("healthy");
      expect(res.body.version).toBe("1.0.0");
      expect(res.body.service).toBe("healthclaw-guardrails");
      expect(res.body.transports).toEqual(
        expect.arrayContaining(["streamable-http", "sse", "http-bridge"])
      );
      expect(res.body.timestamp).toBeDefined();
    });

    it("reports active session counts", async () => {
      const res = await request(app).get("/health").expect(200);
      expect(res.body.activeSessions).toBeDefined();
      expect(typeof res.body.activeSessions.streamableHttp).toBe("number");
      expect(typeof res.body.activeSessions.sse).toBe("number");
    });

    it("reports CORS mode", async () => {
      const res = await request(app).get("/health").expect(200);
      expect(res.body.cors).toBeDefined();
      expect(res.body.cors.mode).toBeDefined();
    });
  });

  // -- Streamable HTTP /mcp --

  describe("POST /mcp", () => {
    it("initialize returns session ID in Mcp-Session-Id header", async () => {
      const res = await request(app)
        .post("/mcp")
        .send({
          jsonrpc: "2.0",
          id: 1,
          method: "initialize",
          params: { protocolVersion: "2024-11-05" },
        });

      expect(res.status).toBe(200);
      expect(res.body.jsonrpc).toBe("2.0");
      expect(res.body.id).toBe(1);
      expect(res.body.result).toBeDefined();
      expect(res.body.result.serverInfo.name).toBe("healthclaw-guardrails");
      expect(res.body.result.serverInfo.version).toBe("1.0.0");
      expect(res.body.result.protocolVersion).toBe("2024-11-05");
      expect(res.body.result.capabilities).toHaveProperty("tools");

      const sessionId = res.headers["mcp-session-id"];
      expect(sessionId).toBeDefined();
      expect(typeof sessionId).toBe("string");
      expect(sessionId.length).toBeGreaterThan(0);
    });

    it("tools/list returns all 16 tool schemas", async () => {
      const res = await request(app)
        .post("/mcp")
        .send({ jsonrpc: "2.0", id: 2, method: "tools/list" });

      expect(res.status).toBe(200);
      expect(res.body.result).toBeDefined();
      expect(res.body.result.tools).toHaveLength(16);

      const names = new Set<string>(
        res.body.result.tools.map((t: { name: string }) => t.name)
      );
      expect(names).toEqual(EXPECTED_TOOL_NAME_SET);
    });

    it("tools/call without session returns 400", async () => {
      const res = await request(app)
        .post("/mcp")
        .send({
          jsonrpc: "2.0",
          id: 3,
          method: "tools/call",
          params: {
            name: "fhir_read",
            arguments: { resource_type: "Patient", resource_id: "pt-1" },
          },
        });

      expect(res.status).toBe(400);
      expect(res.body.error).toBeDefined();
      expect(res.body.error.message).toContain("session");
    });

    it("tools/call with valid session executes the tool", async () => {
      // Step 1: initialize to get session ID
      const initRes = await request(app)
        .post("/mcp")
        .send({
          jsonrpc: "2.0",
          id: 1,
          method: "initialize",
          params: { protocolVersion: "2024-11-05" },
        });
      const sessionId = initRes.headers["mcp-session-id"];

      // Step 2: call a tool with the session
      const fhirPatient = { resourceType: "Patient", id: "pt-1" };
      mockFetch.mockResolvedValueOnce(fakeResponse(fhirPatient));

      const res = await request(app)
        .post("/mcp")
        .set("Mcp-Session-Id", sessionId)
        .send({
          jsonrpc: "2.0",
          id: 4,
          method: "tools/call",
          params: {
            name: "fhir_read",
            arguments: { resource_type: "Patient", resource_id: "pt-1" },
          },
        });

      expect(res.status).toBe(200);
      expect(res.body.result).toBeDefined();
      expect(res.body.result.content).toHaveLength(1);
      expect(res.body.result.content[0].type).toBe("text");
      const parsed = JSON.parse(res.body.result.content[0].text);
      expect(parsed.resourceType).toBe("Patient");
    });

    it("invalid JSON-RPC (no jsonrpc field) returns 400", async () => {
      const res = await request(app)
        .post("/mcp")
        .send({ not: "jsonrpc" });

      expect(res.status).toBe(400);
      expect(res.body.error).toBeDefined();
      expect(res.body.error.message).toContain("Invalid JSON-RPC");
    });

    it("unknown method returns JSON-RPC method-not-found error", async () => {
      const res = await request(app)
        .post("/mcp")
        .send({ jsonrpc: "2.0", id: 10, method: "unknown/method" });

      // JSON-RPC errors use HTTP 200 with error in body
      expect(res.status).toBe(200);
      expect(res.body.error).toBeDefined();
      expect(res.body.error.code).toBe(-32601);
      expect(res.body.error.message).toContain("Method not found");
    });

    it("notifications/initialized returns 204", async () => {
      const res = await request(app)
        .post("/mcp")
        .send({ jsonrpc: "2.0", method: "notifications/initialized" });

      expect(res.status).toBe(204);
    });
  });

  // -- DELETE /mcp (session cleanup) --

  describe("DELETE /mcp", () => {
    it("cleans up session and returns 204", async () => {
      // Create a session first
      const initRes = await request(app)
        .post("/mcp")
        .send({
          jsonrpc: "2.0",
          id: 1,
          method: "initialize",
          params: { protocolVersion: "2024-11-05" },
        });
      const sessionId = initRes.headers["mcp-session-id"];
      expect(sessionId).toBeDefined();

      // Delete the session
      const deleteRes = await request(app)
        .delete("/mcp")
        .set("Mcp-Session-Id", sessionId);
      expect(deleteRes.status).toBe(204);

      // Verify the session is gone: tools/call should fail with 400
      const callRes = await request(app)
        .post("/mcp")
        .set("Mcp-Session-Id", sessionId)
        .send({
          jsonrpc: "2.0",
          id: 5,
          method: "tools/call",
          params: {
            name: "fhir_read",
            arguments: { resource_type: "Patient", resource_id: "pt-1" },
          },
        });
      expect(callRes.status).toBe(400);
      expect(callRes.body.error.message).toContain("session");
    });
  });

  // -- SSE transport --

  describe("GET /sse", () => {
    it("starts SSE connection with text/event-stream content type", (done) => {
      request(app)
        .get("/sse")
        .buffer(false)
        .parse((res: any, callback: any) => {
          expect(res.headers["content-type"]).toContain("text/event-stream");
          let data = "";
          res.on("data", (chunk: Buffer) => {
            data += chunk.toString();
            if (data.length > 0) {
              res.destroy();
            }
          });
          res.on("end", () => callback(null, data));
          res.on("error", () => callback(null, data));
          setTimeout(() => {
            res.destroy();
            callback(null, data);
          }, 500);
        })
        .end(() => {
          done();
        });
    });
  });

  // -- CORS preflight --

  describe("CORS", () => {
    it("OPTIONS /mcp returns 204 for preflight", async () => {
      const res = await request(app).options("/mcp");
      expect(res.status).toBe(204);
    });
  });

  // -- Legacy HTTP Bridge /mcp/rpc --

  describe("POST /mcp/rpc", () => {
    it("tools/list returns all 16 tool schemas", async () => {
      const res = await request(app)
        .post("/mcp/rpc")
        .send({ jsonrpc: "2.0", id: 1, method: "tools/list" });

      expect(res.status).toBe(200);
      expect(res.body.result.tools).toHaveLength(16);
    });

    it("tools/call executes the tool and returns result directly (not wrapped)", async () => {
      const fhirPatient = { resourceType: "Patient", id: "pt-1" };
      mockFetch.mockResolvedValueOnce(fakeResponse(fhirPatient));

      const res = await request(app)
        .post("/mcp/rpc")
        .send({
          jsonrpc: "2.0",
          id: 1,
          method: "tools/call",
          params: {
            name: "fhir_read",
            arguments: { resource_type: "Patient", resource_id: "pt-1" },
          },
        });

      expect(res.status).toBe(200);
      expect(res.body.result.resourceType).toBe("Patient");
    });

    it("invalid JSON-RPC returns 400", async () => {
      const res = await request(app)
        .post("/mcp/rpc")
        .send({ jsonrpc: "1.0", method: "" });

      expect(res.status).toBe(400);
      expect(res.body.error.code).toBe(-32600);
    });

    it("unknown RPC method returns method-not-found error", async () => {
      const res = await request(app)
        .post("/mcp/rpc")
        .send({ jsonrpc: "2.0", id: 99, method: "nonexistent" });

      expect(res.body.error.code).toBe(-32601);
      expect(res.body.error.message).toContain("Method not found");
    });
  });
});
