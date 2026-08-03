/**
 * Drift guard for `adapters/tools.manifest.json`.
 *
 * The manifest is what OpenAI and Gemini callers see. It was hand-maintained
 * and drifted: `rx_transfer_request` lost its `inputSchema`, `title` and
 * `annotations` entirely (so the bridge emitted an empty parameter schema and
 * every model call failed validation), and `action_commit` still described the
 * pre-v1.8.0 "executes the action" semantics after commit became
 * submit-for-approval. Both are the kind of error that only shows up in a
 * stranger's client.
 *
 * These tests make the committed file a build artifact: change a tool, run
 * `npm run manifest`, or CI fails.
 */

import * as fs from "fs";
import {
  buildToolManifest,
  MANIFEST_PATH,
  MANIFEST_SOURCE,
  serializeToolManifest,
} from "./manifest";

function committed(): Record<string, unknown> {
  return JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf-8"));
}

describe("adapters/tools.manifest.json", () => {
  it("deep-equals freshly generated output", () => {
    expect(committed()).toEqual(buildToolManifest());
  });

  it("is byte-identical to the generator's output (run `npm run manifest`)", () => {
    expect(fs.readFileSync(MANIFEST_PATH, "utf-8")).toBe(serializeToolManifest());
  });

  it("carries the source label and a tool_count that matches the tool list", () => {
    const manifest = committed() as { source: string; tool_count: number; tools: unknown[] };
    expect(manifest.source).toBe(MANIFEST_SOURCE);
    expect(manifest.tool_count).toBe(manifest.tools.length);
  });

  it("gives every tool a title, description and non-empty inputSchema", () => {
    // The S-6 regression in one assertion: an entry without `inputSchema`
    // becomes `parameters: {"type": "object"}` in healthclaw_bridge.py.
    const manifest = committed() as {
      tools: Array<{
        name: string;
        title?: string;
        description?: string;
        inputSchema?: Record<string, unknown>;
        annotations?: Record<string, unknown>;
      }>;
    };
    expect(manifest.tools.length).toBeGreaterThan(0);
    for (const tool of manifest.tools) {
      expect(typeof tool.title).toBe("string");
      expect(typeof tool.description).toBe("string");
      expect(tool.annotations).toBeDefined();
      expect(tool.inputSchema).toBeDefined();
      expect(tool.inputSchema!.type).toBe("object");
      expect(tool.inputSchema).toHaveProperty("properties");
    }
  });

  it("includes the privileged tools, because it is the catalogue not a transport view", () => {
    const names = (committed() as { tools: Array<{ name: string }> }).tools.map((t) => t.name);
    expect(names).toContain("fhir_get_token");
    expect(names).toContain("fhir_seed");
  });
});
