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
  "action_commit",
  "action_propose",
  "action_status",
  "context_get",
  "curatr_apply_fix",
  "curatr_evaluate",
  "fetch",
  "fhir_commit_write",
  "fhir_compiled_truth",
  "fhir_get_token",
  "fhir_interpret_labs",
  "fhir_lastn",
  "fhir_permission_evaluate",
  "fhir_propose_write",
  "fhir_read",
  "fhir_search",
  "fhir_seed",
  "fhir_stats",
  "fhir_subscription_topics",
  "fhir_validate",
  "questionnaire_extract",
  "questionnaire_populate",
  "search",
  "shl_generate",
  "sources_check",
  "wearables_sync_status",
];

const EXPECTED_TOOL_NAME_SET = new Set(EXPECTED_TOOL_NAMES);

const READ_ONLY_TOOL_NAMES = [
  "action_status",
  "context_get",
  "fetch",
  "fhir_read",
  "fhir_search",
  "fhir_validate",
  "fhir_stats",
  "fhir_interpret_labs",
  "fhir_lastn",
  "fhir_permission_evaluate",
  "fhir_subscription_topics",
  "fhir_compiled_truth",
  "questionnaire_populate",
  "search",
  "sources_check",
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

  it("getMCPToolSchemas() returns exactly 26 tools", () => {
    expect(schemas).toHaveLength(26);
  });

  it("exposes questionnaire_populate (read) and questionnaire_extract (write)", () => {
    const defs = tools.getToolSchemas();
    const pop = defs.find((t) => t.name === "questionnaire_populate");
    const ext = defs.find((t) => t.name === "questionnaire_extract");
    expect(pop).toBeDefined();
    expect(pop!.tier).toBe("read");
    expect(ext).toBeDefined();
    expect(ext!.tier).toBe("write");
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

  it("every tool has a human-readable title (connector-directory requirement)", () => {
    for (const tool of schemas) {
      expect(typeof tool.title).toBe("string");
      expect((tool.title as string).length).toBeGreaterThan(0);
    }
  });

  it("all 26 tool names match the expected set", () => {
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

  // -- fhir_get_token tenant binding (regression: tokens were always minted
  //    for desktop-demo, ignoring X-Tenant-Id → "Token tenant mismatch" on
  //    writes for any other tenant, e.g. the personas' ev-personal) --

  it("fhir_get_token binds the token to the X-Tenant-Id header when no arg given", async () => {
    mockFetch.mockResolvedValueOnce(fakeResponse({ token: "tok-abc" }));

    const result = await tools.executeTool(
      "fhir_get_token",
      {},
      { "x-tenant-id": "ev-personal" }
    );

    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toContain("/internal/step-up-token");
    expect(JSON.parse(opts.body).tenant_id).toBe("ev-personal");
    expect((result as Record<string, unknown>).tenant_id).toBe("ev-personal");
  });

  it("fhir_get_token honors an explicit tenant_id argument over the header", async () => {
    mockFetch.mockResolvedValueOnce(fakeResponse({ token: "tok-xyz" }));

    await tools.executeTool(
      "fhir_get_token",
      { tenant_id: "explicit-tenant" },
      { "x-tenant-id": "ev-personal" }
    );

    expect(JSON.parse(mockFetch.mock.calls[0][1].body).tenant_id).toBe("explicit-tenant");
  });

  // -- ensureReadToken / READ_TOKEN_AUTOMINT (read-path consumer hardening) --

  it("read tool makes NO mint call when READ_TOKEN_AUTOMINT is unset (current behavior)", async () => {
    const prev = process.env.READ_TOKEN_AUTOMINT;
    delete process.env.READ_TOKEN_AUTOMINT;

    const fhirPatient = { resourceType: "Patient", id: "pt-1" };
    mockFetch.mockResolvedValueOnce(fakeResponse(fhirPatient));

    try {
      await tools.executeTool(
        "fhir_read",
        { resource_type: "Patient", resource_id: "pt-1" },
        { "x-tenant-id": "no-automint-tenant" }
      );

      // Exactly one fetch — the read itself, no mint.
      expect(mockFetch).toHaveBeenCalledTimes(1);
      const [url, opts] = mockFetch.mock.calls[0];
      expect(url).toBe(`${BASE}/Patient/pt-1`);
      expect(opts.headers["X-Step-Up-Token"]).toBeUndefined();
    } finally {
      if (prev === undefined) delete process.env.READ_TOKEN_AUTOMINT;
      else process.env.READ_TOKEN_AUTOMINT = prev;
    }
  });

  it("read tool mints a token first then forwards it when READ_TOKEN_AUTOMINT=true and no incoming token", async () => {
    const prevAutomint = process.env.READ_TOKEN_AUTOMINT;
    const prevSecret = process.env.INTERNAL_TOKEN_MINT_SECRET;
    process.env.READ_TOKEN_AUTOMINT = "true";
    process.env.INTERNAL_TOKEN_MINT_SECRET = "mint-secret-xyz";
    // Unique tenant so the module-level token cache starts cold for this test.
    const tenant = `automint-${Date.now()}`;

    const fhirPatient = { resourceType: "Patient", id: "pt-1" };
    mockFetch
      .mockResolvedValueOnce(fakeResponse({ token: "minted-read-tok" })) // mint
      .mockResolvedValueOnce(fakeResponse(fhirPatient)); // read

    try {
      const result = await tools.executeTool(
        "fhir_read",
        { resource_type: "Patient", resource_id: "pt-1" },
        { "x-tenant-id": tenant }
      );

      expect(mockFetch).toHaveBeenCalledTimes(2);

      // Call 1: mint endpoint with internal secret + tenant.
      const [mintUrl, mintOpts] = mockFetch.mock.calls[0];
      expect(mintUrl).toBe(`${BASE}/internal/step-up-token`);
      expect(mintOpts.method).toBe("POST");
      expect(mintOpts.headers["X-Internal-Secret"]).toBe("mint-secret-xyz");
      expect(JSON.parse(mintOpts.body).tenant_id).toBe(tenant);

      // Call 2: the actual read, now carrying the minted token.
      const [readUrl, readOpts] = mockFetch.mock.calls[1];
      expect(readUrl).toBe(`${BASE}/Patient/pt-1`);
      expect(readOpts.headers["X-Step-Up-Token"]).toBe("minted-read-tok");

      expect(result).toEqual(fhirPatient);
    } finally {
      if (prevAutomint === undefined) delete process.env.READ_TOKEN_AUTOMINT;
      else process.env.READ_TOKEN_AUTOMINT = prevAutomint;
      if (prevSecret === undefined) delete process.env.INTERNAL_TOKEN_MINT_SECRET;
      else process.env.INTERNAL_TOKEN_MINT_SECRET = prevSecret;
    }
  });

  it("read tool with caller-provided step-up token does NOT mint (token left untouched)", async () => {
    const prevAutomint = process.env.READ_TOKEN_AUTOMINT;
    process.env.READ_TOKEN_AUTOMINT = "true";

    const fhirPatient = { resourceType: "Patient", id: "pt-1" };
    mockFetch.mockResolvedValueOnce(fakeResponse(fhirPatient));

    try {
      await tools.executeTool(
        "fhir_read",
        { resource_type: "Patient", resource_id: "pt-1" },
        { "x-tenant-id": "byo-token-tenant", "x-step-up-token": "caller-token" }
      );

      // No mint call — caller already supplied a token.
      expect(mockFetch).toHaveBeenCalledTimes(1);
      const [url, opts] = mockFetch.mock.calls[0];
      expect(url).toBe(`${BASE}/Patient/pt-1`);
      expect(opts.headers["X-Step-Up-Token"]).toBe("caller-token");
    } finally {
      if (prevAutomint === undefined) delete process.env.READ_TOKEN_AUTOMINT;
      else process.env.READ_TOKEN_AUTOMINT = prevAutomint;
    }
  });

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

  // -- fhir_interpret_labs --

  it("fhir_interpret_labs posts to /Observation/$interpret and returns Parameters", async () => {
    mockFetch.mockResolvedValueOnce(
      fakeResponse({ resourceType: "Parameters", parameter: [] })
    );

    const result = await tools.executeTool(
      "fhir_interpret_labs",
      { observation: { resourceType: "Observation" } },
      { "x-tenant-id": "t1" }
    );

    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toContain(`${BASE}/Observation/$interpret`);
    expect(opts.method).toBe("POST");
    expect((result as Record<string, unknown>).resourceType).toBe("Parameters");
  });

  it("fhir_interpret_labs appends ?subject= when a subject reference is given", async () => {
    mockFetch.mockResolvedValueOnce(
      fakeResponse({ resourceType: "Parameters", parameter: [] })
    );

    await tools.executeTool(
      "fhir_interpret_labs",
      { subject: "Patient/pt-1" },
      { "x-tenant-id": "t1" }
    );

    const [url] = mockFetch.mock.calls[0];
    expect(url).toContain("subject=Patient%2Fpt-1");
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

  // -- action_propose --

  it("action_propose forwards tenant header and posts to /r6/actions/propose", async () => {
    const draft = { id: "act-001", kind: "phone-call", status: "proposed", script: "Hi, this is a call." };
    mockFetch.mockResolvedValueOnce(fakeResponse(draft));

    const result = await tools.executeTool(
      "action_propose",
      { kind: "phone-call", payload: { to: "Dr. Smith", phone: "+15551234567", body: "Requesting referral." } },
      { "x-tenant-id": "tenant-xyz" }
    );

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toContain("/r6/actions/propose");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-Tenant-Id"]).toBe("tenant-xyz");
    const body = JSON.parse(opts.body);
    expect(body.kind).toBe("phone-call");
    expect(result).toEqual(draft);
  });

  // -- action_commit without step-up --

  it("action_commit requires step-up token (returns error without it, no fetch made)", async () => {
    const result = await tools.executeTool(
      "action_commit",
      { action_id: "act-001" },
      {} // no step-up token
    );

    expect(result).toHaveProperty("error", "Step-up authorization required");
    expect(result).toHaveProperty("requires_step_up", true);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("action_commit returns step-up error when headers are undefined", async () => {
    const result = await tools.executeTool(
      "action_commit",
      { action_id: "act-001" }
      // headers argument omitted
    );

    expect(result).toHaveProperty("error", "Step-up authorization required");
    expect(result).toHaveProperty("requires_step_up", true);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  // -- action_commit with step-up --

  it("action_commit with step-up token sends X-Step-Up-Token and X-Human-Confirmed: true", async () => {
    const committed = { id: "act-001", status: "executing" };
    mockFetch.mockResolvedValueOnce(fakeResponse(committed));

    const result = await tools.executeTool(
      "action_commit",
      { action_id: "act-001" },
      { "x-step-up-token": "valid-token-abc", "x-tenant-id": "tenant-xyz" }
    );

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toContain("/r6/actions/act-001/commit");
    expect(opts.method).toBe("POST");
    expect(opts.headers["X-Step-Up-Token"]).toBe("valid-token-abc");
    expect(opts.headers["X-Human-Confirmed"]).toBe("true");
    expect(result).toEqual(committed);
  });

  it("action_commit on 410 returns error + detail containing 'expired'", async () => {
    const body = { error: "Proposal expired — propose the action again" };
    mockFetch.mockResolvedValueOnce(fakeResponse(body, 410));

    const result = await tools.executeTool(
      "action_commit",
      { action_id: "act-expired" },
      { "x-step-up-token": "valid-token-abc", "x-tenant-id": "tenant-xyz" }
    );

    expect(result).toHaveProperty("error");
    expect((result.error as string)).toContain("410");
    expect(result).toHaveProperty("detail");
    const detail = result.detail as Record<string, unknown>;
    expect((detail.error as string).toLowerCase()).toContain("expired");
    expect(result).not.toHaveProperty("requires_step_up");
  });

  it("action_commit on 401 includes requires_step_up: true", async () => {
    const body = { error: "Step-up token rejected: token expired" };
    mockFetch.mockResolvedValueOnce(fakeResponse(body, 401));

    const result = await tools.executeTool(
      "action_commit",
      { action_id: "act-001" },
      { "x-step-up-token": "expired-token", "x-tenant-id": "tenant-xyz" }
    );

    expect(result).toHaveProperty("error");
    expect((result.error as string)).toContain("401");
    expect(result).toHaveProperty("requires_step_up", true);
    expect(result).toHaveProperty("detail");
  });

  // -- action_status --

  it("action_status fetches /r6/actions/<id> and returns parsed status", async () => {
    const statusBody = { id: "act-001", status: "completed", outcome: "sent" };
    mockFetch.mockResolvedValueOnce(fakeResponse(statusBody));

    const result = await tools.executeTool(
      "action_status",
      { action_id: "act-001" },
      { "x-tenant-id": "tenant-xyz" }
    );

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url] = mockFetch.mock.calls[0];
    expect(url).toContain("/r6/actions/act-001");
    expect(url).not.toContain("/commit");
    expect(result).toEqual(statusBody);
  });

  // -- sources_check --

  it("sources_check calls the sources-summary URL with the tenant and returns the parsed summary", async () => {
    const summary = {
      tenant: "ev-personal",
      total_records: 177,
      connected_count: 3,
      source_count: 7,
      sources: [
        { id: "fasten", name: "Fasten", connected: true, detail: "", last_activity: null },
        { id: "medent", name: "MEDENT", connected: true, detail: "", last_activity: null },
      ],
      records_by_type: [
        { type: "Condition", count: 57 },
        { type: "Observation", count: 120 },
      ],
    };
    mockFetch.mockResolvedValueOnce(fakeResponse(summary));

    const result = await tools.executeTool(
      "sources_check",
      {},
      { "x-tenant-id": "ev-personal" }
    );

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url] = mockFetch.mock.calls[0];
    expect(url).toContain("/command-center/api/sources-summary");
    expect(url).toContain("tenant=ev-personal");
    expect(result).toHaveProperty("sources");
    expect(result).toHaveProperty("_mcp_summary");
    expect(result._mcp_summary as string).toContain("3 of 7 sources connected");
  });

  it("sources_check forwards X-Step-Up-Token when provided", async () => {
    const summary = { tenant: "ev-personal", total_records: 0, connected_count: 0, source_count: 7, sources: [], records_by_type: [] };
    mockFetch.mockResolvedValueOnce(fakeResponse(summary));

    await tools.executeTool(
      "sources_check",
      {},
      { "x-tenant-id": "ev-personal", "x-step-up-token": "tok-stepup-1" }
    );

    const [, opts] = mockFetch.mock.calls[0];
    expect(opts.headers["X-Step-Up-Token"]).toBe("tok-stepup-1");
    expect(opts.headers["X-Tenant-Id"]).toBe("ev-personal");
  });

  it("sources_check on 401 returns requires_step_up", async () => {
    mockFetch.mockResolvedValueOnce(fakeResponse({ error: "unauthorized" }, 401));

    const result = await tools.executeTool(
      "sources_check",
      {},
      { "x-tenant-id": "ev-personal" }
    );

    expect(result).toHaveProperty("error");
    expect((result.error as string)).toContain("401");
    expect(result).toHaveProperty("requires_step_up", true);
  });

  // -- shl_generate --

  it("shl_generate without step-up token returns requires_step_up (no fetch made)", async () => {
    const result = await tools.executeTool(
      "shl_generate",
      { label: "Test Clinic" },
      {} // no step-up token
    );

    expect(result).toHaveProperty("error", "Step-up authorization required");
    expect(result).toHaveProperty("requires_step_up", true);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("shl_generate simulation mode: SHL_SERVER_URL unset → simulated: true, no second fetch", async () => {
    const prevShl = process.env.SHL_SERVER_URL;
    delete process.env.SHL_SERVER_URL;

    const bundle = {
      resourceType: "Bundle",
      type: "collection",
      entry: [{ resource: { resourceType: "Patient" } }, { resource: { resourceType: "Observation" } }],
    };
    mockFetch.mockResolvedValueOnce(fakeResponse(bundle));

    try {
      const result = await tools.executeTool(
        "shl_generate",
        { label: "Clinic Visit", profile: "intake" },
        { "x-step-up-token": "valid-token-abc", "x-tenant-id": "tenant-xyz" }
      );

      // Only the share-bundle fetch should have been made (not SHL server)
      expect(mockFetch).toHaveBeenCalledTimes(1);
      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain("$share-bundle");

      expect(result).toHaveProperty("simulated", true);
      expect(result).toHaveProperty("shlink", "shlink:/SIMULATED");
      expect(result).toHaveProperty("resource_count", 2);
      expect((result.note as string)).toContain("2 resources");
    } finally {
      if (prevShl !== undefined) process.env.SHL_SERVER_URL = prevShl;
    }
  });

  it("shl_generate real mode: creates shlink with correct structure, manage_link, and JWE upload", async () => {
    const prevShl = process.env.SHL_SERVER_URL;
    process.env.SHL_SERVER_URL = "http://shl.test";

    const bundle = {
      resourceType: "Bundle",
      type: "collection",
      entry: [{ resource: { resourceType: "Patient" } }],
    };
    const linkData = { id: "abc", url: "http://shl.test/shl/abc" };
    const fileData = { fileId: "f1" };

    mockFetch
      .mockResolvedValueOnce(fakeResponse(bundle))          // share-bundle
      .mockResolvedValueOnce(fakeResponse(linkData))        // POST /api/links
      .mockResolvedValueOnce(fakeResponse(fileData));       // POST /api/manage/files

    try {
      const result = await tools.executeTool(
        "shl_generate",
        { label: "Records for Winters Healthcare", expires_in_days: 14 },
        { "x-step-up-token": "valid-token-abc", "x-tenant-id": "tenant-xyz" }
      );

      expect(mockFetch).toHaveBeenCalledTimes(3);

      // Verify /api/links call had Bearer auth
      const [linksUrl, linksOpts] = mockFetch.mock.calls[1];
      expect(linksUrl).toContain("/api/links");
      expect((linksOpts.headers as Record<string, string>)["Authorization"]).toMatch(/^Bearer /);

      // Verify /api/manage/files POST body is a 5-segment JWE
      const [filesUrl, filesOpts] = mockFetch.mock.calls[2];
      expect(filesUrl).toContain("/api/manage/files");
      const jweBody = filesOpts.body as string;
      expect(jweBody.split(".").length).toBe(5);
      expect((filesOpts.headers as Record<string, string>)["Content-Type"]).toBe("application/jose");

      // Verify returned shlink parses with correct url and flag
      expect(result).not.toHaveProperty("error");
      const shlink = result.shlink as string;
      expect(typeof shlink).toBe("string");
      expect(shlink).toMatch(/^shlink:\//);

      // Parse the shlink and verify fields
      const { parseShlink } = await import("./ktc/shlink");
      const parsed = parseShlink(shlink);
      expect(parsed.url).toBe("http://shl.test/shl/abc");
      expect(parsed.flag).toBe("U");
      expect(typeof parsed.key).toBe("string");
      expect(parsed.key.length).toBeGreaterThan(0);

      // manage_link starts with base URL + /m#
      const manageLink = result.manage_link as string;
      expect(manageLink).toMatch(/^http:\/\/shl\.test\/m#/);

      // viewer_link contains the shlink
      expect(result.viewer_link as string).toContain(shlink);

      expect(result.resource_count).toBe(1);
    } finally {
      if (prevShl !== undefined) process.env.SHL_SERVER_URL = prevShl;
      else delete process.env.SHL_SERVER_URL;
    }
  });

  // -- search / fetch (ChatGPT connector compatibility) --

  it("search returns compact results shape from a mocked Bundle", async () => {
    const bundle = {
      resourceType: "Bundle",
      type: "searchset",
      total: 1,
      entry: [
        {
          resource: {
            resourceType: "Observation",
            id: "obs-1",
            code: { coding: [{ display: "Glucose" }] },
          },
        },
      ],
    };
    mockFetch.mockResolvedValueOnce(fakeResponse(bundle));

    const result = await tools.executeTool("search", { query: "Observation?code=4548-4" });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url] = mockFetch.mock.calls[0];
    expect(url).toContain(`${BASE}/Observation?`);
    expect(url).toContain("code=4548-4");
    expect(url).toContain("_count=20");

    expect(result).toEqual({
      results: [
        {
          id: "Observation/obs-1",
          title: "Observation: Glucose",
          url: `${BASE}/Observation/obs-1`,
        },
      ],
    });
  });

  it("search with a bare resource type (no '?') works", async () => {
    const bundle = { resourceType: "Bundle", type: "searchset", total: 0, entry: [] };
    mockFetch.mockResolvedValueOnce(fakeResponse(bundle));

    const result = await tools.executeTool("search", { query: "Patient" });

    const [url] = mockFetch.mock.calls[0];
    expect(url).toBe(`${BASE}/Patient?_count=20`);
    expect(result).toEqual({ results: [] });
  });

  it("search summarizes a Patient result using the name text", async () => {
    const bundle = {
      resourceType: "Bundle",
      type: "searchset",
      total: 1,
      entry: [
        { resource: { resourceType: "Patient", id: "pt-1", name: [{ text: "Jane Doe" }] } },
      ],
    };
    mockFetch.mockResolvedValueOnce(fakeResponse(bundle));

    const result = await tools.executeTool("search", { query: "Patient?name=jane" });

    expect(result).toEqual({
      results: [
        { id: "Patient/pt-1", title: "Patient: Jane Doe", url: `${BASE}/Patient/pt-1` },
      ],
    });
  });

  it("search clamps a caller-provided _count above MAX_RESULT_ENTRIES (50)", async () => {
    const bundle = { resourceType: "Bundle", type: "searchset", total: 0, entry: [] };
    mockFetch.mockResolvedValueOnce(fakeResponse(bundle));

    await tools.executeTool("search", { query: "Patient?_count=999" });

    const [url] = mockFetch.mock.calls[0];
    expect(url).toContain("_count=50");
  });

  it("search returns an error object when the upstream search fails", async () => {
    mockFetch.mockResolvedValueOnce(fakeResponse({}, 500));

    const result = await tools.executeTool("search", { query: "Patient" });

    expect(result).toHaveProperty("error");
    expect((result.error as string)).toContain("500");
  });

  it("fetch returns document shape for a valid ResourceType/id", async () => {
    const patient = {
      resourceType: "Patient",
      id: "pt-1",
      name: [{ text: "Jane Doe" }],
      meta: { lastUpdated: "2026-01-01T00:00:00Z" },
    };
    mockFetch.mockResolvedValueOnce(fakeResponse(patient));

    const result = await tools.executeTool("fetch", { id: "Patient/pt-1" });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url] = mockFetch.mock.calls[0];
    expect(url).toBe(`${BASE}/Patient/pt-1`);

    expect(result).toEqual({
      id: "Patient/pt-1",
      title: "Patient: Jane Doe",
      text: JSON.stringify(patient),
      url: `${BASE}/Patient/pt-1`,
      metadata: { resourceType: "Patient", lastUpdated: "2026-01-01T00:00:00Z" },
    });
  });

  it("fetch falls back to code.coding[0].display, then resource id, for the title", async () => {
    const obs = { resourceType: "Observation", id: "obs-2", code: { coding: [{ display: "Hemoglobin A1c" }] } };
    mockFetch.mockResolvedValueOnce(fakeResponse(obs));

    const result = await tools.executeTool("fetch", { id: "Observation/obs-2" });
    expect((result as Record<string, unknown>).title).toBe("Observation: Hemoglobin A1c");

    mockFetch.mockResolvedValueOnce(fakeResponse({ resourceType: "Condition", id: "cond-1" }));
    const result2 = await tools.executeTool("fetch", { id: "Condition/cond-1" });
    expect((result2 as Record<string, unknown>).title).toBe("Condition: cond-1");
  });

  it("fetch with malformed id (no 'ResourceType/id' format) returns an error and makes no request", async () => {
    const result = await tools.executeTool("fetch", { id: "not-a-valid-id" });

    expect(result).toHaveProperty("error");
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("fetch propagates a 404 from the upstream read as an error object", async () => {
    mockFetch.mockResolvedValueOnce(fakeResponse({}, 404));

    const result = await tools.executeTool("fetch", { id: "Patient/nonexistent" });

    expect(result).toHaveProperty("error");
    expect((result.error as string)).toContain("404");
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

    it("tools/list returns all 26 tool schemas", async () => {
      const res = await request(app)
        .post("/mcp")
        .send({ jsonrpc: "2.0", id: 2, method: "tools/list" });

      expect(res.status).toBe(200);
      expect(res.body.result).toBeDefined();
      expect(res.body.result.tools).toHaveLength(26);

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
    it("tools/list returns all 26 tool schemas", async () => {
      const res = await request(app)
        .post("/mcp/rpc")
        .send({ jsonrpc: "2.0", id: 1, method: "tools/list" });

      expect(res.status).toBe(200);
      expect(res.body.result.tools).toHaveLength(26);
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
