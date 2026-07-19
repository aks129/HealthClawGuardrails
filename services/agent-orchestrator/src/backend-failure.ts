import type { Response } from "node-fetch";

const MAX_BACKEND_ERROR_BYTES = 64 * 1024;
const MAX_OPERATION_OUTCOME_ISSUES = 5;

const SAFE_SEVERITIES = new Set(["fatal", "error", "warning", "information"]);
const SAFE_ISSUE_CODES = new Set([
  "invalid",
  "structure",
  "required",
  "value",
  "invariant",
  "security",
  "login",
  "unknown",
  "expired",
  "forbidden",
  "suppressed",
  "processing",
  "not-supported",
  "duplicate",
  "multiple-matches",
  "not-found",
  "deleted",
  "too-long",
  "code-invalid",
  "extension",
  "too-costly",
  "business-rule",
  "conflict",
  "transient",
  "lock-error",
  "no-store",
  "exception",
  "timeout",
  "incomplete",
  "throttled",
  "informational",
]);

const LOCAL_SEARCH_PARAMETERS = [
  "patient",
  "code",
  "status",
  "_lastUpdated",
  "_count",
  "_sort",
  "_summary",
  "context-id",
] as const;
const AUDIT_SEARCH_PARAMETERS = ["context-id", "entity-type", "_count"] as const;
const LOCAL_SEARCH_PARAMETER_SETS = [
  LOCAL_SEARCH_PARAMETERS,
  AUDIT_SEARCH_PARAMETERS,
] as const;
const SAFE_UNSUPPORTED_SEARCH_KEYS = new Set(["date", "datetime"]);
const SAFE_MODIFIER_TOKENS = new Set([
  "above",
  "below",
  "contains",
  "exact",
  "identifier",
  "in",
  "iterate",
  "missing",
  "not",
  "not-in",
  "of-type",
  "text",
  "type",
  "frobnicate",
]);
const FIXED_LOCAL_SEARCH_MESSAGES = new Set([
  ...[...new Set([...LOCAL_SEARCH_PARAMETERS, ...AUDIT_SEARCH_PARAMETERS])].map(
    (parameter) => `Repeated ${parameter} parameters are not supported.`
  ),
  "_count must be a non-negative integer.",
  "_sort must be _lastUpdated or -_lastUpdated.",
  "_summary only supports count.",
  "Patient reference must match Patient/{id}.",
  "_lastUpdated must be a valid ISO datetime with optional ge/le/gt/lt prefix.",
  "context-id must be a valid FHIR id.",
]);

const ISSUE_CODE_MESSAGES: Record<string, string> = {
  invalid: "The FHIR backend rejected the request as invalid.",
  structure: "The FHIR backend rejected the request structure.",
  required: "The FHIR backend reported a missing required element.",
  value: "The FHIR backend rejected a submitted value.",
  "not-supported": "The FHIR backend does not support this request.",
  security: "FHIR backend authentication or authorization failed.",
  forbidden: "The FHIR backend forbade this request.",
  "not-found": "The FHIR backend reported the resource was not found.",
  conflict: "The request conflicted with the current FHIR backend state.",
  duplicate: "The FHIR backend reported a duplicate.",
  "too-costly": "The FHIR backend refused the request as too costly.",
  throttled: "The FHIR backend is rate-limiting requests.",
  processing: "The FHIR backend could not process the request.",
  transient: "The FHIR backend could not complete the request.",
  timeout: "The FHIR backend timed out.",
  exception: "The FHIR backend encountered an internal error.",
};

interface SanitizedIssue {
  severity: string;
  code: string;
  details: { text: string };
}

interface SanitizedOperationOutcome {
  resourceType: "OperationOutcome";
  issue: SanitizedIssue[];
}

function issueCodeForStatus(status: number): string {
  const statusCodes: Record<number, string> = {
    400: "invalid",
    401: "security",
    403: "security",
    404: "not-found",
    405: "not-supported",
    409: "conflict",
    410: "deleted",
    412: "conflict",
    422: "processing",
    429: "throttled",
  };
  return statusCodes[status] ?? (status >= 500 ? "transient" : "processing");
}

function messageForCode(code: string): string {
  return ISSUE_CODE_MESSAGES[code] ?? "The FHIR backend could not complete the request.";
}

function safeLocalSearchText(value: unknown): string | undefined {
  if (typeof value !== "string" || value.length > 1_000) return undefined;
  if (FIXED_LOCAL_SEARCH_MESSAGES.has(value)) return value;

  const suffix = LOCAL_SEARCH_PARAMETER_SETS.map(
    (parameters) => `. Supported parameters: ${parameters.join(", ")}.`
  ).find((candidate) => value.endsWith(candidate));
  if (!suffix) return undefined;
  const prefix = value.slice(0, -suffix.length);
  if (prefix === "Unknown parameter" || prefix === "Unsupported modifier") {
    return value;
  }

  const unknown = /^Unknown parameter: ([A-Za-z_-]+)$/.exec(prefix);
  if (unknown && SAFE_UNSUPPORTED_SEARCH_KEYS.has(unknown[1])) return value;

  const modifier = /^Unsupported modifier: ([A-Za-z_-]+):([A-Za-z-]+)$/.exec(prefix);
  if (
    modifier &&
    LOCAL_SEARCH_PARAMETERS.includes(
      modifier[1] as (typeof LOCAL_SEARCH_PARAMETERS)[number]
    ) &&
    SAFE_MODIFIER_TOKENS.has(modifier[2])
  ) {
    return value;
  }
  return undefined;
}

function fallbackOutcome(status: number): SanitizedOperationOutcome {
  const code = issueCodeForStatus(status);
  return {
    resourceType: "OperationOutcome",
    issue: [{ severity: "error", code, details: { text: messageForCode(code) } }],
  };
}

function sanitizeOperationOutcome(
  value: unknown,
  status: number
): SanitizedOperationOutcome | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const outcome = value as Record<string, unknown>;
  if (outcome.resourceType !== "OperationOutcome" || !Array.isArray(outcome.issue)) {
    return undefined;
  }

  const fallbackCode = issueCodeForStatus(status);
  const issues: SanitizedIssue[] = [];
  for (const rawIssue of outcome.issue.slice(0, MAX_OPERATION_OUTCOME_ISSUES)) {
    if (!rawIssue || typeof rawIssue !== "object" || Array.isArray(rawIssue)) continue;
    const issue = rawIssue as Record<string, unknown>;
    const tokensAreSafe =
      typeof issue.severity === "string" &&
      SAFE_SEVERITIES.has(issue.severity) &&
      typeof issue.code === "string" &&
      SAFE_ISSUE_CODES.has(issue.code);
    const severity = tokensAreSafe ? (issue.severity as string) : "error";
    const code = tokensAreSafe ? (issue.code as string) : fallbackCode;
    const details = issue.details;
    const localSearchText =
      tokensAreSafe && details && typeof details === "object" && !Array.isArray(details)
        ? safeLocalSearchText((details as Record<string, unknown>).text)
        : undefined;
    issues.push({
      severity,
      code,
      details: { text: localSearchText ?? messageForCode(code) },
    });
  }

  return issues.length > 0
    ? { resourceType: "OperationOutcome", issue: issues }
    : undefined;
}

async function readBoundedJson(response: Response): Promise<unknown> {
  const contentLength = response.headers?.get("content-length");
  if (contentLength) {
    const size = Number(contentLength);
    if (Number.isFinite(size) && size > MAX_BACKEND_ERROR_BYTES) return undefined;
  }

  try {
    let text: string;
    const body = response.body as (AsyncIterable<Uint8Array | string> & {
      destroy?: () => void;
    }) | null;
    if (body && typeof body[Symbol.asyncIterator] === "function") {
      const chunks: Buffer[] = [];
      let size = 0;
      for await (const chunk of body) {
        const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        size += bytes.length;
        if (size > MAX_BACKEND_ERROR_BYTES) {
          body.destroy?.();
          return undefined;
        }
        chunks.push(bytes);
      }
      text = Buffer.concat(chunks, size).toString("utf8");
    } else {
      // Response-like mocks and adapters may not expose a readable body.
      text = await response.text();
      if (Buffer.byteLength(text, "utf8") > MAX_BACKEND_ERROR_BYTES) return undefined;
    }
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

export async function backendFailureResult(
  response: Response
): Promise<Record<string, unknown>> {
  const body = await readBoundedJson(response);
  const operationOutcome =
    sanitizeOperationOutcome(body, response.status) ?? fallbackOutcome(response.status);
  return {
    error: operationOutcome,
    status: response.status,
  };
}
