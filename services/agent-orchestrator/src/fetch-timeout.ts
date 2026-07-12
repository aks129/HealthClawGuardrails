/**
 * fetchWithTimeout -- single choke-point for every HTTP call the MCP tool
 * layer makes to the Flask backend (and the SHL server).
 *
 * Why: node-fetch has NO default timeout. A cold or wedged backend used to
 * hang the tool call until the MCP client gave up, which reads as a dead
 * demo. Every call site in tools.ts now goes through this helper, which
 * enforces a budget via AbortSignal.timeout() (Node 18+; repo runs Node 22).
 *
 * On abort we throw a typed BackendTimeoutError. executeTool converts it
 * (via backendTimeoutResult) into a structured, non-retry-bait tool result --
 * the raw AbortError / DOMException never reaches the model.
 */

import fetch from "node-fetch";
import type { RequestInit, Response } from "node-fetch";

/** Default per-request budget for backend calls. */
export const DEFAULT_TIMEOUT_MS = 15_000;

/** Typed error thrown when a backend call exceeds its budget. */
export class BackendTimeoutError extends Error {
  readonly timeoutMs: number;

  constructor(timeoutMs: number) {
    super(
      `The HealthClaw backend timed out after ${Math.round(timeoutMs / 1000)}s. ` +
        "The service may be cold-starting; try once more in ~30 seconds, or check service status."
    );
    this.name = "BackendTimeoutError";
    this.timeoutMs = timeoutMs;
  }
}

/**
 * Structured tool result for a backend timeout. Deliberately calm and
 * non-retry-bait: one suggested retry after a delay, not an invitation to
 * hammer the backend.
 */
export function backendTimeoutResult(e: BackendTimeoutError): Record<string, unknown> {
  return {
    error: "backend_timeout",
    retryable: true,
    timeout_ms: e.timeoutMs,
    message: e.message,
  };
}

/**
 * fetch with an enforced timeout. Resolves like fetch; throws
 * BackendTimeoutError if the budget elapses first. All other errors
 * (connection refused, DNS, ...) pass through untouched.
 */
export async function fetchWithTimeout(
  url: string,
  init: RequestInit = {},
  timeoutMs: number = DEFAULT_TIMEOUT_MS
): Promise<Response> {
  const signal = AbortSignal.timeout(timeoutMs);
  try {
    // Cast: node-fetch v2 declares its own structural AbortSignal interface;
    // the global (Node 22 / DOM lib) AbortSignal satisfies it at runtime.
    return await fetch(url, { ...init, signal: signal as unknown as RequestInit["signal"] });
  } catch (e) {
    const name = (e as Error | undefined)?.name;
    if (signal.aborted || name === "AbortError" || name === "TimeoutError") {
      throw new BackendTimeoutError(timeoutMs);
    }
    throw e;
  }
}
