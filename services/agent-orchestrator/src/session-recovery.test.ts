/**
 * An expired session must be recoverable by the client.
 *
 * Reported live by a clinical reviewer mid-conversation: "Give me a summary of the
 * health record" produced three tool calls, all of which came back as
 * execution errors, while the tool list rendered fine. The model's own
 * reading was that the deployment was down. It was not: the demo endpoint
 * answered initialize and a full fhir_search inside the same minute.
 *
 * What actually happened is that her session had ended — sessions live in an
 * in-memory Map with a 30-minute idle TTL, so they end on every redeploy and
 * every pause in a conversation — and the server answered the next tools/call
 * with HTTP 400.
 *
 * Per the MCP Streamable HTTP spec, an unrecognised Mcp-Session-Id MUST get a
 * 404, and a client receiving 404 MUST start a new session by re-initialising.
 * That exchange is the whole recovery path. A 400 is a client error the client
 * cannot act on, so Claude surfaced it as a failed tool call and stopped.
 *
 * tools/list needs no session, which is why the tools stayed visible
 * throughout and the failure read as a broken server rather than an expired
 * cookie.
 */

import request from "supertest";
import { app, closeMCPServerForTests } from "./index";

afterAll(async () => {
  await closeMCPServerForTests();
});

async function initialize(): Promise<string> {
  const res = await request(app)
    .post("/mcp")
    .send({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "t", version: "1" } },
    });
  expect(res.status).toBe(200);
  const sessionId = res.headers["mcp-session-id"];
  expect(typeof sessionId).toBe("string");
  return sessionId as string;
}

function callTool(sessionId?: string) {
  const req = request(app).post("/mcp");
  if (sessionId !== undefined) req.set("Mcp-Session-Id", sessionId);
  return req.send({
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    params: { name: "fhir_search", arguments: { resource_type: "Patient" } },
  });
}

describe("an ended session tells the client how to recover", () => {
  it("answers 404 for a session id the server does not know", async () => {
    // MUTATION: change the status back to 400 -> red. This is the bug.
    const res = await callTool("11111111-2222-3333-4444-555555555555");
    expect(res.status).toBe(404);
  });

  it("answers 404 for a session that existed and then ended", async () => {
    // The lived case: initialise, have the session go away (redeploy, TTL),
    // then keep using the id the client is still holding.
    const sessionId = await initialize();
    await request(app).delete("/mcp").set("Mcp-Session-Id", sessionId);

    const res = await callTool(sessionId);
    expect(res.status).toBe(404);
    expect(String(res.body?.error?.message)).toMatch(/re-initiali/i);
  });

  it("still answers 400 when the client never initialised at all", async () => {
    // Not the same failure and not recoverable by re-initialising a session
    // that was never created. Kept distinct so the 404 keeps meaning
    // "yours ended", which is the only thing that triggers recovery.
    const res = await callTool(undefined);
    expect(res.status).toBe(400);
  });

  it("keeps listing tools without a session, which is why this hid", async () => {
    // Pins the asymmetry that made the failure read as a dead deployment.
    // If tools/list ever starts requiring a session this test should be
    // rewritten deliberately, not deleted.
    const res = await request(app)
      .post("/mcp")
      .send({ jsonrpc: "2.0", id: 3, method: "tools/list" });
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body?.result?.tools)).toBe(true);
  });
});
