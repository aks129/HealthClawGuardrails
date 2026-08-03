/**
 * The declared tier is the step-up gate.
 *
 * The gate used to read `tool.tier === "write" && (name === "fhir_commit_write"
 * || name === "action_commit" || name === "shl_generate")`. The tier looked
 * like it was doing the work; the name list actually was, and five write-tier
 * tools were never gated centrally. A tool added with `tier: "write"` inherited
 * no protection at all — the failure mode is silent, which is the shape the
 * 2026-08-02 retro is about.
 *
 * This suite pins the property rather than the list: every write-tier tool the
 * registry declares is gated, and the fixture map below must cover exactly the
 * write-tier set, so adding a write tool without a fixture fails here.
 */

import { FHIRTools } from "./tools";

jest.mock("node-fetch", () => jest.fn());
import fetch from "node-fetch";
const mockFetch = fetch as unknown as jest.Mock;

/** Ajv validation runs before the gate, so each fixture must be schema-valid. */
const VALID_INPUT: Record<string, Record<string, unknown>> = {
  questionnaire_extract: { questionnaire_response: { resourceType: "QuestionnaireResponse" } },
  fhir_propose_write: {
    resource: { resourceType: "Observation", status: "final" },
    operation: "create",
  },
  fhir_commit_write: {
    resource: { resourceType: "Observation", status: "final" },
    operation: "create",
  },
  curatr_apply_fix: {
    resource_type: "Condition",
    resource_id: "c-1",
    fixes: [{ field_path: "Condition.code.coding[0].system", new_value: "x" }],
    patient_intent: "fix my record",
  },
  action_propose: {
    kind: "phone-call",
    payload: { to: "Dr. Smith", phone: "+15551234567", body: "Requesting referral." },
  },
  rx_transfer_request: {
    to_pharmacy_name: "Corner Pharmacy",
    to_pharmacy_phone: "+15551230000",
  },
  action_commit: { action_id: "act-001" },
  shl_generate: { label: "Records for the clinic" },
};

const READ_TIER_SPOT_CHECKS: Record<string, Record<string, unknown>> = {
  fhir_read: { resource_type: "Patient", resource_id: "p-1" },
  curatr_evaluate: { resource_type: "Condition", resource_id: "c-1" },
  action_status: { action_id: "act-001" },
};

describe("step-up gate follows the declared tier", () => {
  let tools: FHIRTools;
  let writeTierNames: string[];

  beforeEach(() => {
    mockFetch.mockReset();
    tools = new FHIRTools("http://localhost:5000/r6/fhir", { allowPrivileged: true });
    writeTierNames = tools
      .getToolSchemas()
      .filter((t) => t.tier === "write")
      .map((t) => t.name);
  });

  it("the fixture map covers exactly the write-tier tools", () => {
    expect(writeTierNames.slice().sort()).toEqual(Object.keys(VALID_INPUT).sort());
  });

  it("every write-tier tool refuses without a step-up token, and makes no backend call", async () => {
    for (const name of writeTierNames) {
      mockFetch.mockReset();
      const result = await tools.executeTool(name, VALID_INPUT[name], {});
      expect({ name, ...result }).toMatchObject({
        name,
        error: "Step-up authorization required",
        requires_step_up: true,
      });
      expect(mockFetch).not.toHaveBeenCalled();
    }
  });

  it("every write-tier tool refuses when the headers argument is omitted entirely", async () => {
    for (const name of writeTierNames) {
      mockFetch.mockReset();
      const result = await tools.executeTool(name, VALID_INPUT[name]);
      expect(result).toHaveProperty("requires_step_up", true);
      expect(mockFetch).not.toHaveBeenCalled();
    }
  });

  it("a write-tier tool with a step-up token reaches the backend", async () => {
    for (const name of writeTierNames) {
      mockFetch.mockReset();
      mockFetch.mockResolvedValue({
        ok: true,
        status: 200,
        json: jest.fn().mockResolvedValue({ id: "x" }),
        text: jest.fn().mockResolvedValue("{}"),
      });
      await tools.executeTool(name, VALID_INPUT[name], {
        "x-step-up-token": "tok-1",
        "x-tenant-id": "tenant-xyz",
      });
      expect(mockFetch.mock.calls.length).toBeGreaterThan(0);
    }
  });

  it("read-tier tools are never gated", async () => {
    for (const [name, input] of Object.entries(READ_TIER_SPOT_CHECKS)) {
      mockFetch.mockReset();
      mockFetch.mockResolvedValue({
        ok: true,
        status: 200,
        json: jest.fn().mockResolvedValue({ resourceType: "Patient" }),
        text: jest.fn().mockResolvedValue("{}"),
      });
      const result = await tools.executeTool(name, input, {});
      expect(result).not.toHaveProperty("requires_step_up");
    }
  });

  // -- The header the MCP server must never mint --
  //
  // curatr_apply_fix used to attach `X-Human-Confirmed: true` to every
  // upstream call. The MCP client cannot know that a human confirmed a
  // clinical write; asserting it on the human's behalf is the whole defect,
  // even though Flask ignores the header on $curatr-apply-fix and requires an
  // audience-bound, operation-bound, nonce-consumed token instead.
  it("curatr_apply_fix never sends X-Human-Confirmed upstream", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: jest.fn().mockResolvedValue({ issues_fixed: 1 }),
      text: jest.fn().mockResolvedValue("{}"),
    });

    await tools.executeTool("curatr_apply_fix", VALID_INPUT.curatr_apply_fix, {
      "x-step-up-token": "tok-1",
      "x-tenant-id": "tenant-xyz",
    });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toContain("$curatr-apply-fix");
    expect(opts.headers).not.toHaveProperty("X-Human-Confirmed");
    expect(Object.keys(opts.headers).map((k) => k.toLowerCase())).not.toContain(
      "x-human-confirmed"
    );
    expect(JSON.stringify(opts.body || "")).not.toContain("X-Human-Confirmed");
  });
});
