/**
 * Tests for fetchWithTimeout -- the single choke-point for all Flask-backend
 * HTTP calls made by the MCP tool layer.
 *
 * Unlike tools.test.ts, this file does NOT mock node-fetch: it runs the real
 * fetch against a local HTTP server so the AbortSignal.timeout() abort path
 * is exercised end-to-end (hanging socket -> AbortError -> BackendTimeoutError).
 */

import http from "http";
import { AddressInfo } from "net";
import {
  fetchWithTimeout,
  BackendTimeoutError,
  backendTimeoutResult,
  DEFAULT_TIMEOUT_MS,
} from "./fetch-timeout";

describe("fetchWithTimeout", () => {
  let fastServer: http.Server;
  let hangingServer: http.Server;
  let fastUrl: string;
  let hangingUrl: string;

  beforeAll((done) => {
    fastServer = http.createServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: true }));
    });
    // Never responds — simulates a cold/stuck Flask backend.
    hangingServer = http.createServer(() => {
      /* hold the socket open forever */
    });
    fastServer.listen(0, () => {
      fastUrl = `http://127.0.0.1:${(fastServer.address() as AddressInfo).port}/`;
      hangingServer.listen(0, () => {
        hangingUrl = `http://127.0.0.1:${(hangingServer.address() as AddressInfo).port}/`;
        done();
      });
    });
  });

  afterAll((done) => {
    // closeAllConnections drops the deliberately-hung sockets so jest exits.
    hangingServer.closeAllConnections();
    fastServer.close(() => hangingServer.close(() => done()));
  });

  it("resolves normally when the backend responds within the timeout", async () => {
    const resp = await fetchWithTimeout(fastUrl, {}, 5_000);
    expect(resp.ok).toBe(true);
    expect(await resp.json()).toEqual({ ok: true });
  });

  it("defaults to a 15s budget", () => {
    expect(DEFAULT_TIMEOUT_MS).toBe(15_000);
  });

  it("throws BackendTimeoutError when the backend hangs past the timeout", async () => {
    await expect(fetchWithTimeout(hangingUrl, {}, 150)).rejects.toBeInstanceOf(
      BackendTimeoutError
    );
  });

  it("the typed error carries the budget and never leaks a raw AbortError", async () => {
    expect.assertions(4);
    try {
      await fetchWithTimeout(hangingUrl, {}, 150);
    } catch (e) {
      const err = e as BackendTimeoutError;
      expect(err.name).toBe("BackendTimeoutError");
      expect(err.timeoutMs).toBe(150);
      expect(err.message).not.toContain("AbortError");
      expect(err.message).not.toContain("aborted");
    }
  });

  it("non-timeout network errors pass through untouched", async () => {
    // Port 1 on localhost: connection refused, not a timeout.
    await expect(
      fetchWithTimeout("http://127.0.0.1:1/", {}, 5_000)
    ).rejects.not.toBeInstanceOf(BackendTimeoutError);
  });
});

describe("backendTimeoutResult", () => {
  it("converts the typed error into the structured, non-retry-bait tool result", () => {
    const result = backendTimeoutResult(new BackendTimeoutError(15_000));
    expect(result.error).toBe("backend_timeout");
    expect(result.retryable).toBe(true);
    expect(result.timeout_ms).toBe(15_000);
    const message = result.message as string;
    expect(message).toContain("timed out after 15s");
    expect(message).toContain("cold-starting");
    expect(message).toContain("try once more in ~30 seconds");
    expect(message).not.toContain("AbortError");
  });

  it("renders the budget in whole seconds (60s for the conformance suite)", () => {
    const result = backendTimeoutResult(new BackendTimeoutError(60_000));
    expect(result.message).toContain("timed out after 60s");
  });
});
