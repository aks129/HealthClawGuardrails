/**
 * The tool manifest that non-MCP frameworks consume.
 *
 * `adapters/tools.manifest.json` feeds `adapters/healthclaw_bridge.py`, which
 * turns each entry into an OpenAI function tool or a Gemini
 * FunctionDeclaration. A tool whose entry is missing `inputSchema` is emitted
 * with `parameters: {"type": "object"}` — the model then calls it with no
 * arguments and Ajv rejects the call server-side. The failure lands at runtime,
 * in someone else's client.
 *
 * `tests/test_docs_tool_catalogue_drift.py` has claimed since it was written
 * that the manifest "is generated from the same catalogue the server serves".
 * Nothing generated it; it was hand-edited, and it drifted. This module is the
 * generator that makes the claim true, and `manifest.test.ts` fails CI when the
 * committed file stops matching it.
 *
 * Regenerate with `npm run manifest` from `services/agent-orchestrator`.
 */

import * as path from "path";
import { FHIRTools, type MCPToolSchema } from "./tools";

/** Label carried in the manifest so a consumer can tell where it came from. */
export const MANIFEST_SOURCE = "healthclaw-mcp";

/** The committed manifest, resolved from this file (src/ and dist/ are the
 * same depth below the repository root). */
export const MANIFEST_PATH = path.resolve(
  __dirname,
  "..",
  "..",
  "..",
  "adapters",
  "tools.manifest.json"
);

export interface ToolManifest {
  source: string;
  tool_count: number;
  tools: MCPToolSchema[];
}

/**
 * Build the manifest from the live tool catalogue.
 *
 * `allowPrivileged: true` on purpose: the manifest is the *catalogue*, not one
 * transport's view of it. Consumers that need the hosted view subtract
 * `PRIVILEGED_TOOL_NAMES` themselves — that is exactly what the Python drift
 * guard does to check the documented "serves N tools" number.
 *
 * The base URL is irrelevant to schema generation (no tool is executed here);
 * it only has to be a well-formed URL for the constructor.
 */
export function buildToolManifest(): ToolManifest {
  const tools = new FHIRTools("http://localhost:5000/r6/fhir", {
    allowPrivileged: true,
  }).getMCPToolSchemas();
  return { source: MANIFEST_SOURCE, tool_count: tools.length, tools };
}

/** The exact bytes the committed manifest should contain. */
export function serializeToolManifest(): string {
  return `${JSON.stringify(buildToolManifest(), null, 2)}\n`;
}
