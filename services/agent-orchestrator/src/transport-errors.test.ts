import http from "http";
import type { AddressInfo } from "net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

jest.mock("node-fetch", () => jest.fn());
import nodeFetch from "node-fetch";
import { app, closeMCPServerForTests } from "./index";

const mockFetch = nodeFetch as unknown as jest.Mock;

jest.setTimeout(30_000);

const localSearchGuidance =
  "Unknown parameter: datetime. Supported parameters: patient, code, status, _lastUpdated, _count, _sort, _summary, context-id.";

const backendOperationOutcome = {
  resourceType: "OperationOutcome",
  issue: [
    {
      severity: "error",
      code: "not-supported",
      details: { text: localSearchGuidance },
    },
  ],
};

const safeOperationOutcome = {
  resourceType: "OperationOutcome",
  issue: [
    {
      severity: "error",
      code: "not-supported",
      details: { text: localSearchGuidance },
    },
  ],
};

function mockedBackendFailure() {
  return {
    ok: false,
    status: 400,
    headers: { get: jest.fn().mockReturnValue(null) },
    json: jest.fn().mockResolvedValue(backendOperationOutcome),
    text: jest.fn().mockResolvedValue(JSON.stringify(backendOperationOutcome)),
  };
}

function listen(server: http.Server): Promise<number> {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve((server.address() as AddressInfo).port);
    });
  });
}

function close(server: http.Server): Promise<void> {
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

function parseTextContent(result: unknown): unknown {
  if (!result || typeof result !== "object" || !("content" in result)) {
    throw new Error("Expected tool result content");
  }
  const content = (result as { content: unknown }).content;
  if (!Array.isArray(content)) throw new Error("Expected tool result content");
  const block = content[0] as { type?: unknown; text?: unknown } | undefined;
  if (!block || block.type !== "text" || typeof block.text !== "string") {
    throw new Error("Expected text tool result");
  }
  return JSON.parse(block.text) as unknown;
}

interface JSONRPCResponse {
  jsonrpc: "2.0";
  id?: string | number;
  result?: Record<string, unknown>;
  error?: { code: number; message: string };
}

interface SSEConnection {
  endpoint: string;
  nextMessage(id: string | number): Promise<JSONRPCResponse>;
  close(): void;
}

function openSSE(baseUrl: string): Promise<SSEConnection> {
  return new Promise((resolve, reject) => {
    const messages: JSONRPCResponse[] = [];
    const waiters = new Map<string | number, (message: JSONRPCResponse) => void>();
    let buffer = "";
    let endpoint = "";
    let response: http.IncomingMessage | undefined;

    const req = http.get(`${baseUrl}/sse`, (res) => {
      response = res;
      res.setEncoding("utf8");
      res.on("data", (chunk: string) => {
        buffer += chunk;
        const frames = buffer.split(/\r?\n\r?\n/);
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const event = /^event:\s*(.+)$/m.exec(frame)?.[1];
          const data = /^data:\s*(.+)$/m.exec(frame)?.[1];
          if (!data) continue;
          if (event === "endpoint") {
            endpoint = new URL(data, baseUrl).toString();
            resolve({
              endpoint,
              nextMessage(id) {
                const queued = messages.find((message) => message.id === id);
                if (queued) return Promise.resolve(queued);
                return new Promise((resolveMessage) => waiters.set(id, resolveMessage));
              },
              close() {
                response?.destroy();
                req.destroy();
              },
            });
          } else if (event === "message") {
            const message = JSON.parse(data) as JSONRPCResponse;
            const waiter = message.id === undefined ? undefined : waiters.get(message.id);
            if (waiter && message.id !== undefined) {
              waiters.delete(message.id);
              waiter(message);
            } else {
              messages.push(message);
            }
          }
        }
      });
      res.once("error", reject);
    });
    req.once("error", reject);
  });
}

async function postJSON(url: string, body: Record<string, unknown>): Promise<Response> {
  return globalThis.fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

type TransportKind = "streamable-http" | "sse" | "stdio";

describe("MCP transport error parity", () => {
  const appServer = http.createServer(app);
  let backendMode: "failure" | "disconnect" | "hang" = "failure";
  const backendServer = http.createServer((_req, res) => {
    if (backendMode === "disconnect") {
      res.destroy();
      return;
    }
    if (backendMode === "hang") return;
    res.writeHead(400, { "content-type": "application/fhir+json" });
    res.end(JSON.stringify(backendOperationOutcome));
  });
  let appPort: number;
  let backendPort: number;

  beforeAll(async () => {
    [appPort, backendPort] = await Promise.all([listen(appServer), listen(backendServer)]);
  });

  afterEach(() => {
    mockFetch.mockReset();
    backendMode = "failure";
  });

  afterAll(async () => {
    closeMCPServerForTests();
    await Promise.all([close(appServer), close(backendServer)]);
  });

  async function callTool(
    kind: TransportKind,
    name: string,
    scenario: "normal" | "timeout" = "normal"
  ): Promise<JSONRPCResponse> {
    if (kind === "streamable-http") {
      const init = await postJSON(`http://127.0.0.1:${appPort}/mcp`, {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: { protocolVersion: "2025-06-18" },
      });
      const sessionId = init.headers.get("mcp-session-id");
      if (!sessionId) throw new Error("Expected Streamable HTTP session id");
      const response = await globalThis.fetch(`http://127.0.0.1:${appPort}/mcp`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "mcp-session-id": sessionId,
        },
        body: JSON.stringify({
          jsonrpc: "2.0",
          id: 2,
          method: "tools/call",
          params: { name, arguments: { resource_type: "Observation" } },
        }),
      });
      return (await response.json()) as JSONRPCResponse;
    }

    if (kind === "sse") {
      const connection = await openSSE(`http://127.0.0.1:${appPort}`);
      try {
        await postJSON(connection.endpoint, {
          jsonrpc: "2.0",
          id: 1,
          method: "initialize",
          params: {
            protocolVersion: "2025-06-18",
            capabilities: {},
            clientInfo: { name: "sse-wire-test", version: "1.0.0" },
          },
        });
        await connection.nextMessage(1);
        await postJSON(connection.endpoint, {
          jsonrpc: "2.0",
          method: "notifications/initialized",
        });
        await postJSON(connection.endpoint, {
          jsonrpc: "2.0",
          id: 2,
          method: "tools/call",
          params: { name, arguments: { resource_type: "Observation" } },
        });
        return await connection.nextMessage(2);
      } finally {
        connection.close();
      }
    }

    const inheritedEnv = Object.fromEntries(
      Object.entries(process.env).filter((entry): entry is [string, string] =>
        typeof entry[1] === "string"
      )
    );
    const client = new Client({ name: "stdio-wire-test", version: "1.0.0" });
    const stdioArgs =
      scenario === "timeout"
        ? [
            "-e",
            [
              "const originalTimeout = AbortSignal.timeout.bind(AbortSignal);",
              "Object.defineProperty(AbortSignal, 'timeout', { value: (ms) => originalTimeout(Math.min(ms, 25)) });",
              "require('ts-node/register');",
              "require('./src/stdio.ts');",
            ].join(" "),
          ]
        : ["-r", "ts-node/register", "src/stdio.ts"];
    const transport = new StdioClientTransport({
      command: process.execPath,
      args: stdioArgs,
      cwd: process.cwd(),
      env: {
        ...inheritedEnv,
        FHIR_BASE_URL: `http://127.0.0.1:${backendPort}/r6/fhir`,
        READ_TOKEN_AUTOMINT: "false",
      },
      stderr: "pipe",
    });
    try {
      await client.connect(transport);
      try {
        const result = await client.callTool({
          name,
          arguments: { resource_type: "Observation" },
        });
        return { jsonrpc: "2.0", id: 2, result };
      } catch (error) {
        const protocolError = error as { code: number; message: string };
        return {
          jsonrpc: "2.0",
          id: 2,
          error: { code: protocolError.code, message: protocolError.message },
        };
      }
    } finally {
      await client.close();
    }
  }

  it.each(["streamable-http", "sse", "stdio"] as const)(
    "returns equivalent isError content through %s",
    async (kind) => {
      mockFetch.mockResolvedValueOnce(mockedBackendFailure());
      const response = await callTool(kind, "fhir_search");

      expect(response.error).toBeUndefined();
      expect(response.result?.isError).toBe(true);
      expect(parseTextContent(response.result)).toEqual({
        error: safeOperationOutcome,
        status: 400,
      });
    }
  );

  it.each(["streamable-http", "sse", "stdio"] as const)(
    "marks backend_timeout as isError through %s",
    async (kind) => {
      const secret = "Patient Jane Doe https://internal.example?token=secret";
      if (kind === "stdio") {
        backendMode = "hang";
      } else {
        mockFetch.mockRejectedValueOnce(
          Object.assign(new Error(secret), { name: "AbortError" })
        );
      }

      const response = await callTool(kind, "fhir_search", "timeout");

      expect(response.error).toBeUndefined();
      expect(response.result?.isError).toBe(true);
      expect(parseTextContent(response.result)).toMatchObject({
        error: "backend_timeout",
        retryable: true,
      });
      expect(JSON.stringify(response)).not.toContain(secret);
    }
  );

  it.each(["streamable-http", "sse", "stdio"] as const)(
    "turns thrown executor failures into sanitized tool errors through %s",
    async (kind) => {
      const secret = "Patient Jane Doe https://internal.example?token=secret";
      if (kind === "stdio") {
        backendMode = "disconnect";
      } else {
        mockFetch.mockRejectedValueOnce(new Error(secret));
      }

      const response = await callTool(kind, "fhir_search");

      expect(response.error).toBeUndefined();
      expect(response.result?.isError).toBe(true);
      expect(parseTextContent(response.result)).toEqual({
        error: {
          resourceType: "OperationOutcome",
          issue: [
            {
              severity: "error",
              code: "exception",
              details: { text: "The tool could not complete the request." },
            },
          ],
        },
      });
      expect(JSON.stringify(response)).not.toContain(secret);
      expect(JSON.stringify(response)).not.toContain("internal.example");
    }
  );

  it.each(["streamable-http", "sse", "stdio"] as const)(
    "keeps unknown tools as protocol errors through %s",
    async (kind) => {
      const response = await callTool(kind, "fhir_nonexistent");

      expect(response.result).toBeUndefined();
      expect(response.error).toMatchObject({ code: -32601 });
    }
  );

  it("keeps non-object Streamable HTTP arguments as a protocol error", async () => {
    const init = await postJSON(`http://127.0.0.1:${appPort}/mcp`, {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {},
    });
    const response = await globalThis.fetch(`http://127.0.0.1:${appPort}/mcp`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "mcp-session-id": init.headers.get("mcp-session-id") ?? "",
      },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: 2,
        method: "tools/call",
        params: { name: "fhir_search", arguments: "not-an-object" },
      }),
    });
    const body = (await response.json()) as JSONRPCResponse;

    expect(body.result).toBeUndefined();
    expect(body.error).toMatchObject({ code: -32602 });
  });

  it("keeps /mcp/rpc raw while enriching its backend failure", async () => {
    mockFetch.mockResolvedValueOnce(mockedBackendFailure());
    const response = await postJSON(`http://127.0.0.1:${appPort}/mcp/rpc`, {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: {
        name: "fhir_search",
        arguments: { resource_type: "Observation" },
      },
    });
    const body = (await response.json()) as JSONRPCResponse;

    expect(body.result).toEqual({
      error: safeOperationOutcome,
      status: 400,
    });
    expect(body.result).not.toHaveProperty("content");
    expect(body.result).not.toHaveProperty("isError");
  });
});
