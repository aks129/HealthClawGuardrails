/**
 * FHIR MCP Tool Definitions and Executor.
 *
 * Supports FHIR R4 US Core v9 (stable) and FHIR R6 ballot3 (experimental).
 *
 * This is a reference implementation demonstrating MCP guardrail patterns
 * for FHIR agent access. Tools add value beyond raw HTTP by:
 * - Providing reasoning/explanations in responses
 * - Enforcing step-up authorization for writes
 * - Adding clinical context to statistical results
 * - Explaining access control decisions
 *
 * Two tiers:
 * - Read-only (no step-up): context.get, fhir.read, fhir.search, fhir.validate,
 *   fhir.stats, fhir.lastn, fhir.permission_evaluate, fhir.subscription_topics,
 *   fhir.compiled_truth, curatr.evaluate
 * - Write (require step-up): fhir.propose_write, fhir.commit_write,
 *   curatr.apply_fix
 *
 * All tools include MCP annotations (readOnlyHint, destructiveHint, openWorldHint).
 */

import fetch from "node-fetch";
import { generateMasterSecret, deriveAuth, deriveKey } from "./ktc/hkdf";
import { encryptJWE } from "./ktc/jwe";
import { buildShlink, buildOwnerLink, buildViewerLink } from "./ktc/shlink";
import { utf8 } from "./ktc/encoding";

export type ToolTier = "read" | "write";

interface ToolAnnotations {
  readOnlyHint: boolean;
  destructiveHint: boolean;
  openWorldHint: boolean;
}

interface ToolDefinition {
  name: string;
  title: string;
  description: string;
  tier: ToolTier;
  annotations: ToolAnnotations;
  inputSchema: Record<string, unknown>;
}

// MCP SDK tool schema format (includes annotations)
export interface MCPToolSchema {
  name: string;
  title?: string;
  description: string;
  inputSchema: Record<string, unknown>;
  annotations: ToolAnnotations;
}

// Cap search results for token safety (marketplace limit: <25k tokens)
const MAX_RESULT_ENTRIES = 50;

// Per-tenant cache for server-minted read tokens (ensureReadToken). Tokens are
// minted with a ~5-min TTL; reuse until ~30s before expiry to avoid minting on
// every read call. Module-level so it survives across tool invocations.
interface CachedReadToken {
  token: string;
  expiresAtMs: number;
}
const READ_TOKEN_CACHE = new Map<string, CachedReadToken>();
const READ_TOKEN_TTL_MS = 5 * 60 * 1000; // assume 5-min server TTL
const READ_TOKEN_SKEW_MS = 30 * 1000; // re-mint 30s before expiry

export class FHIRTools {
  private baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  /**
   * Env-gated read-token auto-mint. Prepares read-path consumers for the Flask
   * READ_AUTH_ENABLED flag: when on, GET reads for non-public tenants need a
   * tenant-bound step-up token. This mints one server-side so MCP reads keep
   * working after the flip — without changing today's behavior.
   *
   * No-op unless READ_TOKEN_AUTOMINT === 'true'. If a step-up token is already
   * present (caller-provided), it is left untouched. On mint failure we log and
   * proceed (the read may 401 if the flag is on, but we never crash).
   */
  async ensureReadToken(fwdHeaders: Record<string, string>): Promise<void> {
    if (process.env.READ_TOKEN_AUTOMINT !== "true") return;
    if (fwdHeaders["X-Step-Up-Token"]) return;

    const tenant = fwdHeaders["X-Tenant-Id"] || "desktop-demo";
    const now = Date.now();

    // Key by serverRoot + tenant: a step-up token is minted by (and only valid
    // against) a specific Flask backend, so a token cached for one backend must
    // never be reused for a request routed to a different backend.
    const cacheKey = `${this.serverRoot()}::${tenant}`;

    const cached = READ_TOKEN_CACHE.get(cacheKey);
    if (cached && cached.expiresAtMs - READ_TOKEN_SKEW_MS > now) {
      fwdHeaders["X-Step-Up-Token"] = cached.token;
      return;
    }

    try {
      const resp = await fetch(`${this.serverRoot()}/r6/fhir/internal/step-up-token`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Tenant-Id": tenant,
          "X-Internal-Secret": process.env.INTERNAL_TOKEN_MINT_SECRET || "",
        },
        body: JSON.stringify({ tenant_id: tenant }),
      });
      if (!resp.ok) {
        console.error(`ensureReadToken: mint failed (status ${resp.status}) for tenant ${tenant}; proceeding without read token`);
        return;
      }
      const data = (await resp.json()) as Record<string, unknown>;
      const token = data.token as string | undefined;
      if (!token) {
        console.error(`ensureReadToken: mint returned no token for tenant ${tenant}; proceeding without read token`);
        return;
      }
      READ_TOKEN_CACHE.set(cacheKey, { token, expiresAtMs: now + READ_TOKEN_TTL_MS });
      fwdHeaders["X-Step-Up-Token"] = token;
    } catch (e) {
      console.error(`ensureReadToken: mint request error (${(e as Error).name}) for tenant ${tenant}; proceeding without read token`);
    }
  }

  /**
   * Return tool schemas in MCP SDK format (for ListToolsRequestSchema handler).
   * Includes annotations required by OpenAI and Anthropic marketplaces.
   */
  getMCPToolSchemas(): MCPToolSchema[] {
    return this.getToolSchemas().map((t) => ({
      name: t.name,
      title: t.title,
      description: t.description,
      inputSchema: t.inputSchema,
      annotations: t.annotations,
    }));
  }

  getToolSchemas(): ToolDefinition[] {
    return [
      {
        name: "context_get",
        title: "Get Health Context",
        description:
          "Retrieve a pre-built context envelope with patient-centric FHIR resources. Returns bounded, policy-stamped, time-limited context.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            context_id: { type: "string", description: "Context envelope ID" },
          },
          required: ["context_id"],
        },
      },
      {
        name: "fhir_read",
        title: "Read FHIR Resource",
        description: "Read a specific FHIR resource by type and ID. Supports FHIR R4 US Core v9 stable resources and FHIR R6 ballot3 experimental resources. Returns redacted resource with PHI protection.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            resource_type: {
              type: "string",
              enum: [
                "Patient",
                "Encounter",
                "Observation",
                "AuditEvent",
                "Consent",
                "Permission",
                "SubscriptionTopic",
                "Subscription",
                "NutritionIntake",
                "NutritionProduct",
                "DeviceAlert",
                "DeviceAssociation",
                "Requirements",
                "ActorDefinition",
                "Condition",
                "Provenance",
                "AllergyIntolerance",
                "Immunization",
                "MedicationRequest",
                "Medication",
                "MedicationDispense",
                "Procedure",
                "DiagnosticReport",
                "CarePlan",
                "CareTeam",
                "Goal",
                "DocumentReference",
                "Location",
                "Organization",
                "Practitioner",
                "PractitionerRole",
                "RelatedPerson",
                "Coverage",
                "ServiceRequest",
                "Specimen",
                "FamilyMemberHistory",
              ],
            },
            resource_id: { type: "string", description: "The resource ID" },
          },
          required: ["resource_type", "resource_id"],
        },
      },
      {
        name: "fhir_search",
        title: "Search FHIR Resources",
        description:
          "Search for FHIR resources. Supports FHIR R4 US Core v9 stable resources and FHIR R6 ballot3 experimental resources. Supports patient, code, status, _lastUpdated, _count, _sort parameters. Returns paginated, redacted Bundle.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            resource_type: {
              type: "string",
              enum: [
                "Patient",
                "Encounter",
                "Observation",
                "AuditEvent",
                "Consent",
                "Permission",
                "SubscriptionTopic",
                "Subscription",
                "NutritionIntake",
                "NutritionProduct",
                "DeviceAlert",
                "DeviceAssociation",
                "Requirements",
                "ActorDefinition",
                "Condition",
                "Provenance",
                "AllergyIntolerance",
                "Immunization",
                "MedicationRequest",
                "Medication",
                "MedicationDispense",
                "Procedure",
                "DiagnosticReport",
                "CarePlan",
                "CareTeam",
                "Goal",
                "DocumentReference",
                "Location",
                "Organization",
                "Practitioner",
                "PractitionerRole",
                "RelatedPerson",
                "Coverage",
                "ServiceRequest",
                "Specimen",
                "FamilyMemberHistory",
              ],
            },
            patient: {
              type: "string",
              description: "Patient reference filter (e.g., 'Patient/pt-1')",
            },
            code: {
              type: "string",
              description: "Code filter — matches code.coding[].code in JSON (e.g., '2339-0' for Glucose)",
            },
            status: {
              type: "string",
              description: "Status filter (e.g., 'final', 'active', 'completed')",
            },
            _lastUpdated: {
              type: "string",
              description: "Date filter with prefix (e.g., 'ge2024-01-01', 'le2024-12-31')",
            },
            _count: {
              type: "integer",
              description: "Max results (1-50, capped for token safety)",
              default: 20,
            },
            _sort: {
              type: "string",
              description: "Sort order: '_lastUpdated' (asc) or '-_lastUpdated' (desc, default)",
            },
          },
          required: ["resource_type"],
        },
      },
      {
        name: "fhir_validate",
        title: "Validate FHIR Resource",
        description:
          "Validate a proposed FHIR R6 resource against structural rules. Returns OperationOutcome.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            resource: {
              type: "object",
              description: "The FHIR resource to validate",
            },
          },
          required: ["resource"],
        },
      },
      {
        name: "questionnaire_populate",
        title: "Pre-fill Health Form",
        description:
          "SDC $populate — pre-fill a Questionnaire for a subject. Returns a QuestionnaireResponse. Read tier; mints a tenant token for non-public tenants.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            questionnaire_id: { type: "string", description: "Stored Questionnaire id" },
            questionnaire: { type: "object", description: "Inline Questionnaire (overrides questionnaire_id)" },
            subject_reference: { type: "string", description: "Subject reference, e.g. 'Patient/p1'" },
          },
          required: ["subject_reference"],
        },
      },
      {
        name: "questionnaire_extract",
        title: "Extract Form Data to FHIR",
        description:
          "SDC $extract — extract FHIR resources from a completed QuestionnaireResponse into a transaction Bundle. Write tier; requires step-up unless dry_run=true.",
        tier: "write",
        annotations: { readOnlyHint: false, destructiveHint: true, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            questionnaire_response: { type: "object", description: "Completed QuestionnaireResponse" },
            questionnaire: { type: "object", description: "The referenced Questionnaire (optional if resolvable by reference)" },
            dry_run: { type: "boolean", description: "Preview the Bundle without committing", default: false },
          },
          required: ["questionnaire_response"],
        },
      },
      {
        name: "fhir_propose_write",
        title: "Propose FHIR Write",
        description:
          "Propose a write — validates the resource and returns a preview. Does NOT commit. Safe to call without step-up authorization.",
        tier: "write",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            resource: {
              type: "object",
              description: "The FHIR resource to write",
            },
            operation: {
              type: "string",
              enum: ["create", "update"],
              description: "Write operation type",
            },
          },
          required: ["resource", "operation"],
        },
      },
      {
        name: "fhir_commit_write",
        title: "Commit FHIR Write",
        description:
          "Commit a previously proposed write. Requires step-up authorization token. This is a destructive operation.",
        tier: "write",
        annotations: { readOnlyHint: false, destructiveHint: true, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            resource: {
              type: "object",
              description: "The FHIR resource to commit",
            },
            operation: {
              type: "string",
              enum: ["create", "update"],
            },
          },
          required: ["resource", "operation"],
        },
      },
      // --- Additional tools (mix of R6-specific and standard FHIR) ---
      {
        name: "fhir_stats",
        title: "Observation Statistics",
        description:
          "Compute statistics (count, min, max, mean) over numeric Observation values. Standard FHIR $stats (since R4). Only supports valueQuantity. Filter by patient and/or code.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            code: {
              type: "string",
              description: "LOINC code to filter Observations (e.g., '2339-0' for Glucose)",
            },
            patient: {
              type: "string",
              description: "Patient reference filter (e.g., 'Patient/pt-1')",
            },
          },
          required: [],
        },
      },
      {
        name: "fhir_interpret_labs",
        title: "Interpret Lab Results",
        description:
          "Interpret lab Observations against reference ranges — flags each value low/normal/high/critical (HL7 v3 ObservationInterpretation) and returns clinician + consumer summaries. Decision support, not diagnosis. Read-tier.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            observation: { type: "object", description: "A single FHIR Observation to interpret" },
            bundle: { type: "object", description: "A FHIR Bundle of Observations to interpret" },
            subject: { type: "string", description: "Patient reference (e.g. 'Patient/pt-1') — interpret the tenant's stored Observations for this subject" },
          },
          required: [],
        },
      },
      {
        name: "care_gaps",
        title: "Preventive Care Gaps",
        description:
          "Check which preventive-care screenings/immunizations a patient may be due for (blood pressure, cholesterol, colorectal/cervical/breast cancer screening, flu, diabetes A1c), from their own connected records. Decision support based on USPSTF/ACIP/ADA guidelines — not a diagnosis or directive.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            subject: { type: "string", description: "Patient reference (e.g. 'Patient/pt-1')" },
          },
          required: [],
        },
      },
      {
        name: "guardrail_conformance",
        title: "Guardrail Conformance Scorecard",
        description:
          "Run the guardrail conformance self-test on the connected HealthClaw deployment and return the graded scorecard (A-F across PHI redaction, immutable audit, step-up auth, human-in-the-loop, tenant isolation, medical disclaimers). Uses synthetic data only. Set fresh=true to force a new run instead of the cached result.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            fresh: {
              type: "boolean",
              description: "Force a fresh probe run instead of the cached (<=10 min old) result",
            },
          },
          required: [],
        },
      },
      {
        name: "fhir_lastn",
        title: "Latest Observations",
        description:
          "Get the last N observations per code. Standard FHIR $lastn (since R4). Returns most recent observations by storage order.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            code: {
              type: "string",
              description: "LOINC code filter",
            },
            patient: {
              type: "string",
              description: "Patient reference filter",
            },
            max: {
              type: "integer",
              description: "Max observations per code (default 1)",
              default: 1,
            },
          },
          required: [],
        },
      },
      {
        name: "fhir_permission_evaluate",
        title: "Evaluate Access Permission",
        description:
          "Evaluate R6 Permission resources for access control decisions. Returns permit/deny based on stored Permission rules. Separates access control (Permission) from consent records (Consent).",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            subject: {
              type: "string",
              description: "Subject reference (e.g., 'Practitioner/dr-1')",
            },
            action: {
              type: "string",
              enum: ["read", "write", "delete"],
              description: "Action to evaluate",
            },
            resource: {
              type: "string",
              description: "Resource reference to evaluate access for",
            },
          },
          required: ["action"],
        },
      },
      {
        name: "fhir_subscription_topics",
        title: "List Subscription Topics",
        description:
          "List available SubscriptionTopics for event-driven subscriptions. R6 moves topic-based subscriptions toward Normative. Agents discover what events they can subscribe to.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {},
          required: [],
        },
      },
      // --- Wearables: connection + sync status surface ---
      {
        name: "wearables_sync_status",
        title: "Wearables Sync Status",
        description:
          "List wearable connections (Garmin, Oura, Polar, Suunto, Whoop, Fitbit, Strava, Ultrahuman) for a tenant, with last sync time, observation count, and status. Use this to tell a patient what's connected, when data last arrived, and surface a connection-management UI (via _meta.ui.resourceUri) so they can connect more providers. Data flows into HealthClaw as FHIR Observations with LOINC codes — agents read it via fhir_search like any other Observation.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            tenant_id: {
              type: "string",
              description: "Tenant to inspect. Defaults to the incoming X-Tenant-Id header.",
            },
          },
          required: [],
        },
      },
      // --- Sources: survey ALL connected health data sources at once ---
      {
        name: "sources_check",
        title: "Check Data Sources",
        description:
          "Survey ALL connected health data sources (Fasten, HealthEx, Health Bank One, MEDENT, Flexpa, Epic/Health Skillz, wearables) at once — returns each source's connection status and the patient's record counts by type. Use when the patient asks what's connected or to check for data across services.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {},
          required: [],
        },
      },
      // --- Compiled Truth: current state + evidence timeline ---
      {
        name: "fhir_compiled_truth",
        title: "Compiled Truth Timeline",
        description:
          "Return the current best understanding of a FHIR resource plus the append-only evidence trail (Provenance entries) of how it got there. Use this before presenting resource-specific facts to a patient — surfaces curation_state and quality_score so the agent can say not just WHAT the record says but WHY it says it. Redacted, audited. Response includes _meta.ui.resourceUri pointing to an embeddable review UI.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            resource_type: {
              type: "string",
              description: "FHIR resource type (e.g. 'Condition', 'AllergyIntolerance')",
            },
            resource_id: {
              type: "string",
              description: "ID of the resource",
            },
          },
          required: ["resource_type", "resource_id"],
        },
      },
      // --- Curatr: patient-facing data quality tools ---
      {
        name: "curatr_evaluate",
        title: "Evaluate Data Quality",
        description:
          "Evaluate a FHIR resource for data quality issues. Checks coding elements against public terminology services (tx.fhir.org for SNOMED/LOINC, NLM for ICD-10-CM, RXNAV for RxNorm) and structural rules. Returns issues in plain language with patient-facing impact descriptions and resolution suggestions. Read-only — no step-up required.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: true },
        inputSchema: {
          type: "object",
          properties: {
            resource_type: {
              type: "string",
              description: "FHIR resource type to evaluate (e.g. 'Condition')",
            },
            resource_id: {
              type: "string",
              description: "ID of the resource to evaluate",
            },
          },
          required: ["resource_type", "resource_id"],
        },
      },
      {
        name: "curatr_apply_fix",
        title: "Apply Data Quality Fix",
        description:
          "Apply patient-approved data quality fixes to a FHIR resource. Creates a linked Provenance record with full attribution. Requires step-up authorization (X-Step-Up-Token) and human confirmation (X-Human-Confirmed: true) for clinical resources like Condition.",
        tier: "write",
        annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            resource_type: {
              type: "string",
              description: "FHIR resource type to fix (e.g. 'Condition')",
            },
            resource_id: {
              type: "string",
              description: "ID of the resource to fix",
            },
            fixes: {
              type: "array",
              description:
                "List of field fixes to apply. Each fix has 'field_path' (dot-notation, e.g. 'Condition.code.coding[0].system') and 'new_value' (the corrected value).",
              items: {
                type: "object",
                properties: {
                  field_path: { type: "string" },
                  new_value: {},
                },
                required: ["field_path", "new_value"],
              },
            },
            patient_intent: {
              type: "string",
              description:
                "Plain-language reason for the fix, provided by the patient (recorded in Provenance).",
            },
          },
          required: ["resource_type", "resource_id", "fixes", "patient_intent"],
        },
      },
      {
        name: "fhir_get_token",
        title: "Mint Step-Up Token",
        description:
          "Get a fresh step-up authorization token for write operations. Call this before fhir_propose_write, fhir_commit_write, or curatr_apply_fix. Tokens expire after 5 minutes. Returns the token string — pass it as _stepUpToken in subsequent write tool calls.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            tenant_id: {
              type: "string",
              description: "Tenant ID to scope the token to",
            },
          },
          required: ["tenant_id"],
        },
      },
      {
        name: "fhir_seed",
        title: "Seed Demo Data",
        description:
          "Seed a tenant with a realistic Patient + Observations + Condition bundle for live testing. Use this at the start of a demo session to populate data. Returns created resource IDs and a ready-to-use step_up_token.",
        tier: "read",
        annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            tenant_id: {
              type: "string",
              description: "Tenant to seed (default: desktop-demo)",
            },
          },
          required: [],
        },
      },
      // --- Real-world action tools (Phase 1: action core) ---
      {
        name: "action_propose",
        title: "Propose Real-World Action",
        description:
          "Propose a real-world action (phone call or SMS) on the patient's behalf. Returns a draft (id + script) the patient MUST review before commit. Does not execute anything.",
        tier: "write",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: true },
        inputSchema: {
          type: "object",
          properties: {
            kind: {
              type: "string",
              enum: ["phone-call", "sms", "insurance-call"],
              description: "Action type",
            },
            payload: {
              type: "object",
              description:
                "Action content: { to: recipient label, phone: number to dial/text, body: call script or message text }",
            },
          },
          required: ["kind", "payload"],
        },
      },
      {
        name: "rx_transfer_request",
        title: "Request Prescription Transfer",
        description:
          "Draft a prescription-transfer request: assembles the patient's active medications and stages a phone call to the RECEIVING pharmacy asking it to pull the prescriptions from the current pharmacy (how US transfers actually work). Schedule II medications are refused (never transferable — new prescription required). Returns a draft the patient MUST review; execute with action_commit after explicit approval.",
        tier: "write",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: true },
        inputSchema: {
          type: "object",
          properties: {
            to_pharmacy_name: { type: "string", description: "Receiving pharmacy name" },
            to_pharmacy_phone: { type: "string", description: "Receiving pharmacy phone number" },
            from_pharmacy_name: { type: "string", description: "Current pharmacy name (optional)" },
            from_pharmacy_phone: { type: "string", description: "Current pharmacy phone (optional)" },
            medication_names: {
              type: "array",
              items: { type: "string" },
              description: "Limit to these medication names (default: all active orders)",
            },
          },
          required: ["to_pharmacy_name", "to_pharmacy_phone"],
        },
      },
      {
        name: "action_commit",
        title: "Commit Real-World Action",
        description:
          "Execute a previously proposed action AFTER the patient has explicitly approved the draft. Requires step-up authorization (call fhir_get_token first; pass as _stepUpToken). Only call this after the patient says yes.",
        tier: "write",
        annotations: { readOnlyHint: false, destructiveHint: true, openWorldHint: true },
        inputSchema: {
          type: "object",
          properties: {
            action_id: { type: "string", description: "ID returned by action_propose" },
          },
          required: ["action_id"],
        },
      },
      {
        name: "action_status",
        title: "Action Status",
        description:
          "Check the status and outcome of an action (proposed/executing/completed/failed). Use after commit to report the result back to the patient.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            action_id: { type: "string", description: "ID returned by action_propose" },
          },
          required: ["action_id"],
        },
      },
      {
        name: "shl_generate",
        title: "Generate SMART Health Link",
        description:
          "Generate a SMART Health Link (shlink:/ QR payload) sharing the patient's record with a clinic. Fetches the guardrailed share-bundle from HealthClaw (step-up required — pass _stepUpToken), encrypts it client-side (the SHL server never sees plaintext), uploads ciphertext, and returns the shlink URI, viewer link, and the patient's private manage link. ALWAYS get the patient's explicit consent before generating, and deliver the manage link ONLY to the patient.",
        tier: "write",
        annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
        inputSchema: {
          type: "object",
          properties: {
            label: { type: "string", description: "Short label shown in SHL viewers (<=80 chars), e.g. 'Records for Winters Healthcare'. No PHI beyond what the patient approves." },
            expires_in_days: { type: "number", description: "Link lifetime in days (default 7, max 90)" },
            profile: { type: "string", enum: ["intake", "deidentified"], description: "intake = identified record for clinic check-in (default); deidentified = strips name/contact/institutional IDs" },
            patient_id: { type: "string", description: "Optional patient id filter for multi-patient tenants" },
          },
          required: [],
        },
      },
      // --- ChatGPT-connector-compatible tools (thin wrappers over fhir_search/fhir_read) ---
      {
        name: "search",
        title: "Search Health Records",
        description:
          "ChatGPT-connector-compatible search over the tenant's FHIR records. Query is a FHIR search string (e.g. 'Observation?code=4548-4' or 'Patient?name=smith'); bare resource type works too. Returns compact results: id, title, url. Reads are PHI-redacted and audit-logged server-side.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            query: {
              type: "string",
              description: "FHIR search string: 'ResourceType?params' or just 'ResourceType'",
            },
          },
          required: ["query"],
        },
      },
      {
        name: "fetch",
        title: "Fetch Health Record",
        description:
          "ChatGPT-connector-compatible fetch of one FHIR resource by id ('ResourceType/id', as returned by search). Returns the full document (PHI-redacted server-side) with metadata.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            id: {
              type: "string",
              description: "Resource reference: 'ResourceType/id'",
            },
          },
          required: ["id"],
        },
      },
    ];
  }

  async executeTool(
    toolName: string,
    input: Record<string, unknown>,
    headers?: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const tool = this.getToolSchemas().find((t) => t.name === toolName);
    if (!tool) {
      return { error: `Unknown tool: ${toolName}` };
    }

    // Enforce step-up for commit_write, action_commit, and shl_generate (releases full record)
    if (tool.tier === "write" && (toolName === "fhir_commit_write" || toolName === "action_commit" || toolName === "shl_generate")) {
      const stepUpToken = headers?.["x-step-up-token"];
      if (!stepUpToken) {
        return {
          error: "Step-up authorization required",
          requires_step_up: true,
          message:
            "Write operations require an X-Step-Up-Token. Provide authorization to proceed.",
        };
      }
    }

    // Build forwarded headers (tenant, auth, agent)
    // X-Tenant-Id is always set: incoming header → TENANT_ID env var → "desktop-demo"
    const tenantId =
      headers?.["x-tenant-id"] ||
      process.env.TENANT_ID ||
      "desktop-demo";

    const fwdHeaders: Record<string, string> = {
      "Content-Type": "application/fhir+json",
      "X-Tenant-Id": tenantId,
    };
    if (headers?.["x-step-up-token"]) fwdHeaders["X-Step-Up-Token"] = headers["x-step-up-token"];
    if (headers?.["x-agent-id"]) fwdHeaders["X-Agent-Id"] = headers["x-agent-id"];
    if (headers?.["authorization"]) fwdHeaders["Authorization"] = headers["authorization"];
    // SHARP-on-MCP context propagation: forward FHIR base URL + SMART access
    // token + optional patient banner so Flask can build a per-request
    // upstream proxy targeting the agent host's FHIR endpoint.
    if (headers?.["x-fhir-server-url"]) fwdHeaders["X-FHIR-Server-URL"] = headers["x-fhir-server-url"];
    if (headers?.["x-fhir-access-token"]) fwdHeaders["X-FHIR-Access-Token"] = headers["x-fhir-access-token"];
    if (headers?.["x-patient-id"]) fwdHeaders["X-Patient-ID"] = headers["x-patient-id"];

    // Read-path consumers: if READ_TOKEN_AUTOMINT is on and this is a read-tier
    // tool with no caller-provided step-up token, mint one server-side so reads
    // survive the Flask READ_AUTH_ENABLED flag flip for non-public tenants.
    // No-op by default (env unset) → today's behavior is unchanged.
    if (tool.tier === "read") {
      await this.ensureReadToken(fwdHeaders);
    }

    // questionnaire_extract dry-run is read-shaped (Flask gates it with read-auth
    // but no step-up); mint a read token so non-public tenants can preview.
    if (toolName === "questionnaire_extract" && input.dry_run === true) {
      await this.ensureReadToken(fwdHeaders);
    }

    switch (toolName) {
      case "context_get":
        return this.getContext(input.context_id as string, fwdHeaders);

      case "fhir_read":
        return this.readResource(
          input.resource_type as string,
          input.resource_id as string,
          fwdHeaders
        );

      case "fhir_search":
        return this.searchResources(
          input.resource_type as string,
          {
            patient: input.patient as string | undefined,
            code: input.code as string | undefined,
            status: input.status as string | undefined,
            _lastUpdated: input._lastUpdated as string | undefined,
            _count: Math.min((input._count as number) || 20, MAX_RESULT_ENTRIES),
            _sort: input._sort as string | undefined,
          },
          fwdHeaders
        );

      case "fhir_validate":
        return this.validateResource(input.resource as Record<string, unknown>, fwdHeaders);

      case "questionnaire_populate":
        return this.populateQuestionnaire(
          input.questionnaire_id as string | undefined,
          input.questionnaire as Record<string, unknown> | undefined,
          input.subject_reference as string,
          fwdHeaders
        );

      case "questionnaire_extract":
        return this.extractQuestionnaire(
          input.questionnaire_response as Record<string, unknown>,
          input.questionnaire as Record<string, unknown> | undefined,
          (input.dry_run as boolean) ?? false,
          fwdHeaders
        );

      case "fhir_propose_write":
        return this.proposeWrite(
          input.resource as Record<string, unknown>,
          input.operation as string,
          fwdHeaders
        );

      case "fhir_commit_write":
        return this.commitWrite(
          input.resource as Record<string, unknown>,
          input.operation as string,
          fwdHeaders
        );

      // Additional tools (mix of R6-specific and standard FHIR)
      case "fhir_stats":
        return this.observationStats(
          input.code as string | undefined,
          input.patient as string | undefined,
          fwdHeaders
        );

      case "fhir_interpret_labs":
        return this.interpretLabs(
          input.observation as Record<string, unknown> | undefined,
          input.bundle as Record<string, unknown> | undefined,
          input.subject as string | undefined,
          fwdHeaders
        );

      case "care_gaps":
        return this.careGaps(input.subject as string | undefined, fwdHeaders);

      case "guardrail_conformance":
        return this.guardrailConformance(input.fresh === true, fwdHeaders);

      case "fhir_lastn":
        return this.observationLastN(
          input.code as string | undefined,
          input.patient as string | undefined,
          (input.max as number) || 1,
          fwdHeaders
        );

      case "fhir_permission_evaluate":
        return this.evaluatePermission(
          input.subject as string | undefined,
          input.action as string,
          input.resource as string | undefined,
          fwdHeaders
        );

      case "fhir_subscription_topics":
        return this.listSubscriptionTopics(fwdHeaders);

      case "sources_check":
        return this.sourcesCheck(tenantId, fwdHeaders);

      case "fhir_compiled_truth":
        return this.compiledTruth(
          input.resource_type as string,
          input.resource_id as string,
          fwdHeaders
        );

      case "wearables_sync_status":
        return this.wearablesSyncStatus(
          (input.tenant_id as string) || tenantId,
          fwdHeaders
        );

      case "curatr_evaluate":
        return this.curatrEvaluate(
          input.resource_type as string,
          input.resource_id as string,
          fwdHeaders
        );

      case "curatr_apply_fix":
        return this.curatrApplyFix(
          input.resource_type as string,
          input.resource_id as string,
          input.fixes as Array<{ field_path: string; new_value: unknown }>,
          input.patient_intent as string,
          fwdHeaders
        );

      case "fhir_get_token": {
        // Token MUST be bound to the same tenant the write will run as.
        // Fall back to the request's X-Tenant-Id (resolved above as
        // header → TENANT_ID env → desktop-demo) — NOT a hardcoded default,
        // or a token minted for desktop-demo gets rejected when the actual
        // call runs as another tenant ("Token tenant mismatch").
        const tokenTenant = (input.tenant_id as string) || tenantId;
        const resp = await fetch(`${this.baseUrl}/internal/step-up-token`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            // Non-public tenants require the mint secret when it's set.
            "X-Internal-Secret": process.env.INTERNAL_TOKEN_MINT_SECRET || "",
            ...fwdHeaders,
          },
          body: JSON.stringify({ tenant_id: tokenTenant }),
        });
        const data = (await resp.json()) as Record<string, unknown>;
        if (!resp.ok) return { error: "Failed to issue token", detail: data };
        return {
          token: data.token,
          tenant_id: tokenTenant,
          expires_in_seconds: 300,
          _mcp_summary: "Step-up token issued (5-min TTL). Pass it as _stepUpToken in fhir_propose_write, fhir_commit_write, action_commit, shl_generate, or curatr_apply_fix.",
        };
      }

      case "fhir_seed": {
        const seedTenant = (input.tenant_id as string) || tenantId;
        const resp = await fetch(`${this.baseUrl}/internal/seed`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...fwdHeaders },
          body: JSON.stringify({ tenant_id: seedTenant }),
        });
        const data = (await resp.json()) as Record<string, unknown>;
        if (!resp.ok) return { error: "Seed failed", detail: data };
        return {
          ...data,
          _mcp_summary: `Seeded ${(data.created as unknown[])?.length ?? 0} resources into tenant '${seedTenant}'. The step_up_token is ready for write operations.`,
        };
      }

      case "rx_transfer_request":
        return this.proposeRxTransfer(input, fwdHeaders);
      case "action_propose":
        return this.proposeAction(
          input.kind as string,
          input.payload as Record<string, unknown>,
          fwdHeaders
        );

      case "action_commit":
        return this.commitAction(input.action_id as string, fwdHeaders);

      case "action_status":
        return this.getActionStatus(input.action_id as string, fwdHeaders);

      case "shl_generate":
        return this.generateShl(input, fwdHeaders);

      case "search":
        return this.connectorSearch(input.query as string, fwdHeaders);

      case "fetch":
        return this.connectorFetch(input.id as string, fwdHeaders);

      default:
        return { error: `Unimplemented tool: ${toolName}` };
    }
  }

  async getContext(
    contextId: string,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const resp = await fetch(
      `${this.baseUrl}/context/${encodeURIComponent(contextId)}`,
      { headers }
    );
    if (!resp.ok) {
      return { error: `Context fetch failed with status ${resp.status}` };
    }
    return (await resp.json()) as Record<string, unknown>;
  }

  private async readResource(
    resourceType: string,
    resourceId: string,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const resp = await fetch(
      `${this.baseUrl}/${encodeURIComponent(resourceType)}/${encodeURIComponent(resourceId)}`,
      { headers }
    );
    if (!resp.ok) {
      return { error: `Read failed with status ${resp.status}` };
    }
    return (await resp.json()) as Record<string, unknown>;
  }

  private async searchResources(
    resourceType: string,
    searchParams: {
      patient?: string;
      code?: string;
      status?: string;
      _lastUpdated?: string;
      _count: number;
      _sort?: string;
    },
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const params = new URLSearchParams();
    if (searchParams.patient) params.set("patient", searchParams.patient);
    if (searchParams.code) params.set("code", searchParams.code);
    if (searchParams.status) params.set("status", searchParams.status);
    if (searchParams._lastUpdated) params.set("_lastUpdated", searchParams._lastUpdated);
    if (searchParams._sort) params.set("_sort", searchParams._sort);
    params.set("_count", searchParams._count.toString());

    const resp = await fetch(
      `${this.baseUrl}/${encodeURIComponent(resourceType)}?${params.toString()}`,
      { headers }
    );
    if (!resp.ok) {
      return { error: `Search failed with status ${resp.status}` };
    }
    const result = (await resp.json()) as Record<string, unknown>;

    // Add agent-useful summary
    const total = result.total as number ?? 0;
    const appliedFilters = Object.entries(searchParams)
      .filter(([k, v]) => v !== undefined && k !== "_count")
      .map(([k, v]) => `${k}=${v}`);

    (result as Record<string, unknown>)._mcp_summary = {
      total,
      filters_applied: appliedFilters.length > 0 ? appliedFilters : ["none"],
      note: total === 0
        ? `No ${resourceType} resources found matching criteria.`
        : `Found ${total} ${resourceType} resource(s). Results are redacted (PHI masked).`,
    };

    return result;
  }

  private async validateResource(
    resource: Record<string, unknown>,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const resourceType = resource.resourceType as string;
    if (!resourceType) {
      return { error: "Resource must have a resourceType" };
    }
    const resp = await fetch(
      `${this.baseUrl}/${encodeURIComponent(resourceType)}/$validate`,
      {
        method: "POST",
        headers,
        body: JSON.stringify(resource),
      }
    );
    if (!resp.ok) {
      return { error: `Validation request failed with status ${resp.status}` };
    }
    return (await resp.json()) as Record<string, unknown>;
  }

  private async proposeWrite(
    resource: Record<string, unknown>,
    operation: string,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const resourceType = resource.resourceType as string;
    const validation = await this.validateResource(resource, headers);

    // Check if validation passed
    const issues = ((validation as Record<string, unknown>).issue as Array<Record<string, unknown>>) || [];
    const errors = issues.filter((i) => i.severity === "error" || i.severity === "fatal");
    const warnings = issues.filter((i) => i.severity === "warning");
    const passed = errors.length === 0;

    // Determine if clinical resource (requires human-in-the-loop)
    const clinicalTypes = new Set([
      "Observation", "Condition", "MedicationRequest", "DiagnosticReport",
      "AllergyIntolerance", "Procedure", "CarePlan", "Immunization",
      "NutritionIntake", "DeviceAlert",
    ]);
    const requiresHumanConfirmation = clinicalTypes.has(resourceType);

    return {
      proposal_status: passed ? "ready" : "invalid",
      operation,
      resource_type: resourceType,
      validation_result: {
        passed,
        error_count: errors.length,
        warning_count: warnings.length,
        issues: validation,
      },
      next_steps: passed
        ? {
            requires_step_up: true,
            requires_human_confirmation: requiresHumanConfirmation,
            message: requiresHumanConfirmation
              ? `${resourceType} is a clinical resource. Commit requires both X-Step-Up-Token AND X-Human-Confirmed: true headers.`
              : `Ready to commit. Provide X-Step-Up-Token header to proceed.`,
          }
        : {
            message: `Validation failed with ${errors.length} error(s). Fix issues before committing.`,
            errors: errors.map((e) => e.diagnostics || e.details),
          },
    };
  }

  private async commitWrite(
    resource: Record<string, unknown>,
    operation: string,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const resourceType = resource.resourceType as string;
    if (!resourceType) {
      return { error: "Resource must have a resourceType" };
    }

    let resp;
    if (operation === "create") {
      resp = await fetch(`${this.baseUrl}/${encodeURIComponent(resourceType)}`, {
        method: "POST",
        headers,
        body: JSON.stringify(resource),
      });
    } else if (operation === "update") {
      const resourceId = resource.id as string;
      if (!resourceId) {
        return { error: "Resource ID required for update" };
      }
      resp = await fetch(
        `${this.baseUrl}/${encodeURIComponent(resourceType)}/${encodeURIComponent(resourceId)}`,
        {
          method: "PUT",
          headers,
          body: JSON.stringify(resource),
        }
      );
    } else {
      return { error: `Unknown operation: ${operation}` };
    }

    return (await resp.json()) as Record<string, unknown>;
  }

  // --- Tool implementations with reasoning ---

  private async observationStats(
    code: string | undefined,
    patient: string | undefined,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const params = new URLSearchParams();
    if (code) params.set("code", code);
    if (patient) params.set("patient", patient);

    const resp = await fetch(
      `${this.baseUrl}/Observation/$stats?${params.toString()}`,
      { headers }
    );
    if (!resp.ok) {
      return { error: `$stats failed with status ${resp.status}` };
    }
    const result = (await resp.json()) as Record<string, unknown>;

    // Add clinical context to help agent interpret results
    const parameters = (result.parameter as Array<Record<string, unknown>>) || [];
    const count = parameters.find((p) => p.name === "count")?.valueInteger as number ?? 0;
    const mean = parameters.find((p) => p.name === "mean")?.valueDecimal as number | undefined;
    const unit = parameters.find((p) => p.name === "unit")?.valueString as string | undefined;

    (result as Record<string, unknown>)._mcp_summary = {
      observation_count: count,
      code_filtered: code || "all",
      patient_filtered: patient || "all",
      note: count === 0
        ? "No numeric observations found matching criteria. Only valueQuantity values are included."
        : `Computed over ${count} observation(s). Mean=${mean} ${unit || ""}. Only numeric valueQuantity values — coded/string/boolean results excluded.`,
      limitations: [
        "Only valueQuantity.value is used (not valueCodeableConcept, valueString, etc.)",
        "No percentile or median calculations",
        "No multi-component observation support",
      ],
    };

    return result;
  }

  private async observationLastN(
    code: string | undefined,
    patient: string | undefined,
    max: number,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const params = new URLSearchParams();
    if (code) params.set("code", code);
    if (patient) params.set("patient", patient);
    params.set("max", max.toString());

    const resp = await fetch(
      `${this.baseUrl}/Observation/$lastn?${params.toString()}`,
      { headers }
    );
    if (!resp.ok) {
      return { error: `$lastn failed with status ${resp.status}` };
    }
    const result = (await resp.json()) as Record<string, unknown>;

    const total = result.total as number ?? 0;
    (result as Record<string, unknown>)._mcp_summary = {
      returned: total,
      max_requested: max,
      note: `Returned ${total} most recent observation(s) by storage order. Sorted by DB insertion, not effectiveDateTime.`,
    };

    return result;
  }

  private async evaluatePermission(
    subject: string | undefined,
    action: string,
    resource: string | undefined,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const resp = await fetch(
      `${this.baseUrl}/Permission/$evaluate`,
      {
        method: "POST",
        headers,
        body: JSON.stringify({ subject, action, resource }),
      }
    );
    if (!resp.ok) {
      return { error: `Permission $evaluate failed with status ${resp.status}` };
    }
    return (await resp.json()) as Record<string, unknown>;
  }

  // --- SDC: Questionnaire $populate / $extract ---

  private async populateQuestionnaire(
    questionnaireId: string | undefined,
    questionnaire: Record<string, unknown> | undefined,
    subjectReference: string,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const parameter: Array<Record<string, unknown>> = [
      { name: "subject", valueReference: { reference: subjectReference } },
    ];
    if (questionnaire) parameter.push({ name: "questionnaire", resource: questionnaire });

    const path = questionnaireId
      ? `/Questionnaire/${encodeURIComponent(questionnaireId)}/$populate`
      : `/Questionnaire/$populate`;
    const resp = await fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify({ resourceType: "Parameters", parameter }),
    });
    if (!resp.ok) {
      return { error: `$populate failed with status ${resp.status}` };
    }
    return (await resp.json()) as Record<string, unknown>;
  }

  private async extractQuestionnaire(
    questionnaireResponse: Record<string, unknown>,
    questionnaire: Record<string, unknown> | undefined,
    dryRun: boolean,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const parameter: Array<Record<string, unknown>> = [
      { name: "questionnaire-response", resource: questionnaireResponse },
    ];
    if (questionnaire) parameter.push({ name: "questionnaire", resource: questionnaire });

    const url = `${this.baseUrl}/QuestionnaireResponse/$extract?dryRun=${dryRun}`;
    const resp = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify({ resourceType: "Parameters", parameter }),
    });
    if (!resp.ok) {
      return { error: `$extract failed with status ${resp.status}` };
    }
    return (await resp.json()) as Record<string, unknown>;
  }

  private async listSubscriptionTopics(
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const resp = await fetch(
      `${this.baseUrl}/SubscriptionTopic/$list`,
      { headers }
    );
    if (!resp.ok) {
      return { error: `SubscriptionTopic $list failed with status ${resp.status}` };
    }
    const result = (await resp.json()) as Record<string, unknown>;

    const total = result.total as number ?? 0;
    (result as Record<string, unknown>)._mcp_summary = {
      topic_count: total,
      note: total === 0
        ? "No SubscriptionTopics found. Create one first."
        : `Found ${total} topic(s). Note: this demo stores topics but does NOT dispatch notifications.`,
    };

    return result;
  }

  // --- Compiled Truth: current state + evidence timeline ---

  /**
   * Build the MCP App URI for the Compiled Truth review page. MCP clients
   * that understand `_meta.ui.resourceUri` render this inline; others
   * treat it as a plain link.
   */
  private compiledTruthAppUri(resourceType: string, resourceId: string): string {
    return `${this.baseUrl}/mcp-apps/compiled-truth/${encodeURIComponent(resourceType)}/${encodeURIComponent(resourceId)}`;
  }

  /**
   * Server root URL (strips trailing /r6/fhir so /wearables/... resolves).
   * baseUrl is always something like http://host:5000/r6/fhir.
   */
  private serverRoot(): string {
    return this.baseUrl.replace(/\/r6\/fhir\/?$/, "");
  }

  private async wearablesSyncStatus(
    tenantId: string,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const root = this.serverRoot();
    const url = `${root}/wearables/sync-status?tenant_id=${encodeURIComponent(tenantId)}`;
    let status: Record<string, unknown>;
    try {
      const resp = await fetch(url, { headers });
      status = (await resp.json()) as Record<string, unknown>;
      if (!resp.ok) {
        return {
          error: `wearables status failed with ${resp.status}`,
          detail: status,
        };
      }
    } catch (e) {
      return {
        error: "wearables status request failed",
        detail: String(e),
      };
    }

    const conns = (status.connections as Array<Record<string, unknown>>) || [];
    const enabled = !!status.enabled;
    const narrative = conns.length
      ? conns
          .map((c) => {
            const provider = c.provider;
            const lastAt = c.last_sync_at as string | null;
            const count = (c.observation_count as number) ?? 0;
            const when = lastAt ? `synced ${this.timeAgo(lastAt)}` : "never synced";
            return `${provider}: ${when}, ${count} observations`;
          })
          .join("; ")
      : "no wearables connected for this tenant";

    status._mcp_summary = {
      tenant_id: tenantId,
      enabled,
      connection_count: conns.length,
      narrative,
      next_steps: enabled
        ? conns.length > 0
          ? [
              "Use fhir_search(resource_type='Observation', code='<LOINC>') to query wearable data",
              "Compiled Truth on a wearable Observation shows device provenance",
              "Open the MCP App to connect more providers",
            ]
          : [
              "Direct the patient to the MCP App to connect a provider",
              "Connections require the operator to set <PROVIDER>_CLIENT_ID env vars",
            ]
        : [
            "Operator has not set OPEN_WEARABLES_URL — integration disabled",
          ],
    };
    status._meta = {
      ui: {
        resourceUri: `${root}/r6/fhir/mcp-apps/wearables/?tenant_id=${encodeURIComponent(tenantId)}`,
        profile: "mcp-app",
      },
    };
    return status;
  }

  private timeAgo(iso: string): string {
    const d = new Date(iso);
    const mins = Math.round((Date.now() - d.getTime()) / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins} min ago`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return `${hrs} h ago`;
    return `${Math.round(hrs / 24)} d ago`;
  }

  // --- Sources: survey ALL connected health data sources at once ---

  private async sourcesCheck(
    tenantId: string,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const root = this.serverRoot();
    const url = `${root}/command-center/api/sources-summary?tenant=${encodeURIComponent(tenantId)}`;
    let resp;
    try {
      resp = await fetch(url, { headers });
    } catch (e) {
      return { error: "sources_check request failed", detail: String(e) };
    }

    let data: Record<string, unknown>;
    try {
      data = (await resp.json()) as Record<string, unknown>;
    } catch {
      data = {};
    }

    if (!resp.ok) {
      const result: Record<string, unknown> = {
        error: `sources_check failed with status ${resp.status}`,
        detail: data,
      };
      if (resp.status === 401) result.requires_step_up = true;
      return result;
    }

    const connected = (data.connected_count as number) ?? 0;
    const sourceCount = (data.source_count as number) ?? 7;
    const totalRecords = (data.total_records as number) ?? 0;
    data._mcp_summary = `${connected} of ${sourceCount} sources connected; ${totalRecords} total records.`;

    return data;
  }

  private async compiledTruth(
    resourceType: string,
    resourceId: string,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const resp = await fetch(
      `${this.baseUrl}/${encodeURIComponent(resourceType)}/${encodeURIComponent(resourceId)}/$compiled-truth`,
      { headers }
    );
    if (!resp.ok) {
      return { error: `Compiled truth failed with status ${resp.status}` };
    }
    const result = (await resp.json()) as Record<string, unknown>;

    // Extract surface summary from the Parameters response for the agent.
    const params = (result.parameter || []) as Array<Record<string, unknown>>;
    const byName = (n: string) =>
      params.find((p) => p.name === n) || ({} as Record<string, unknown>);
    const state = byName("curation_state").valueString as string | undefined;
    const score = byName("quality_score").valueDecimal as number | undefined;
    const count = byName("timeline_count").valueInteger as number | undefined;
    const reviewNeeded = byName("review_needed").valueBoolean as boolean | undefined;

    result._mcp_summary = {
      resource: `${resourceType}/${resourceId}`,
      curation_state: state ?? "raw",
      quality_score: score ?? 1.0,
      timeline_events: count ?? 0,
      review_needed: reviewNeeded ?? false,
      note: (count ?? 0) === 0
        ? "No corrections recorded yet. This is the raw record."
        : `Record has ${count} recorded correction(s). The agent can narrate what changed, when, and why.`,
      patient_facing: [
        "Say WHAT the record currently says (the 'current' parameter).",
        "Say WHY it says that (cite the timeline — recorded + agent + reason).",
        "If review_needed=true, suggest reviewing outstanding quality issues.",
      ],
    };
    result._meta = {
      ui: {
        resourceUri: this.compiledTruthAppUri(resourceType, resourceId),
        profile: "mcp-app",
      },
    };
    return result;
  }

  private async interpretLabs(
    observation: Record<string, unknown> | undefined,
    bundle: Record<string, unknown> | undefined,
    subject: string | undefined,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const params = new URLSearchParams();
    if (subject) params.set("subject", subject);
    const query = params.toString();

    const resp = await fetch(
      `${this.baseUrl}/Observation/$interpret${query ? `?${query}` : ""}`,
      {
        method: "POST",
        headers,
        body: JSON.stringify(bundle || observation || {}),
      }
    );
    if (!resp.ok) {
      return { error: `$interpret failed with status ${resp.status}` };
    }
    return (await resp.json()) as Record<string, unknown>;
  }

  private async careGaps(
    subject: string | undefined,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const params = new URLSearchParams();
    if (subject) params.set("subject", subject);
    const query = params.toString();

    const resp = await fetch(
      `${this.baseUrl}/Patient/$care-gaps${query ? `?${query}` : ""}`,
      { method: "POST", headers, body: JSON.stringify({}) }
    );
    if (!resp.ok) {
      return { error: `$care-gaps failed with status ${resp.status}` };
    }
    return (await resp.json()) as Record<string, unknown>;
  }

  // --- Curatr: patient-facing data quality tools ---

  private async guardrailConformance(
    fresh: boolean,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const url = `${this.baseUrl}/$conformance${fresh ? "?fresh=1" : ""}`;
    const resp = await fetch(url, { headers });
    // 503 = graded below A — still return the scorecard body, it explains why.
    const body = (await resp.json()) as Record<string, unknown>;
    if (!resp.ok && !("grade" in body)) {
      return { error: `Conformance self-test failed with status ${resp.status}` };
    }
    return body;
  }

  private async curatrEvaluate(
    resourceType: string,
    resourceId: string,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const resp = await fetch(
      `${this.baseUrl}/${encodeURIComponent(resourceType)}/${encodeURIComponent(resourceId)}/$curatr-evaluate`,
      { headers }
    );
    if (!resp.ok) {
      return { error: `Curatr evaluate failed with status ${resp.status}` };
    }
    const result = (await resp.json()) as Record<string, unknown>;

    const issueCount = result.issue_count as number ?? 0;
    const quality = result.overall_quality as string ?? "unknown";

    (result as Record<string, unknown>)._mcp_summary = {
      resource: `${resourceType}/${resourceId}`,
      overall_quality: quality,
      issue_count: issueCount,
      note: issueCount === 0
        ? `No data quality issues found in this ${resourceType} record.`
        : `Found ${issueCount} issue(s). Present each issue to the patient in plain language before calling curatr.apply_fix.`,
      next_steps: issueCount > 0
        ? [
            "Present each issue.plain_language and issue.impact to the patient",
            "Show issue.suggestion for each issue",
            "Ask patient which fixes they approve",
            "Call curatr.apply_fix with approved fixes and patient_intent",
          ]
        : ["No action needed — data quality looks good."],
      public_services_used: [
        "tx.fhir.org (SNOMED CT, LOINC)",
        "NLM Clinical Tables API (ICD-10-CM)",
        "RXNAV API (RxNorm)",
      ],
    };
    // Link to the Compiled Truth MCP App so the agent can surface a
    // review UI straight from a quality check.
    (result as Record<string, unknown>)._meta = {
      ui: {
        resourceUri: this.compiledTruthAppUri(resourceType, resourceId),
        profile: "mcp-app",
      },
    };

    return result;
  }

  private async curatrApplyFix(
    resourceType: string,
    resourceId: string,
    fixes: Array<{ field_path: string; new_value: unknown }>,
    patientIntent: string,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const stepUpToken = headers["X-Step-Up-Token"] || headers["x-step-up-token"];
    if (!stepUpToken) {
      return {
        error: "Step-up authorization required for curatr.apply_fix",
        requires_step_up: true,
        message:
          "Applying fixes to clinical resources requires X-Step-Up-Token and X-Human-Confirmed: true headers.",
      };
    }

    const resp = await fetch(
      `${this.baseUrl}/${encodeURIComponent(resourceType)}/${encodeURIComponent(resourceId)}/$curatr-apply-fix`,
      {
        method: "POST",
        headers: { ...headers, "X-Human-Confirmed": "true" },
        body: JSON.stringify({ fixes, patient_intent: patientIntent }),
      }
    );
    if (!resp.ok) {
      return { error: `Curatr apply-fix failed with status ${resp.status}` };
    }
    const result = (await resp.json()) as Record<string, unknown>;

    const fixed = result.issues_fixed as number ?? 0;
    (result as Record<string, unknown>)._mcp_summary = {
      resource: `${resourceType}/${resourceId}`,
      fixes_applied: fixed,
      provenance_created: !!(result.provenance),
      note: `${fixed} fix(es) applied. A Provenance resource was created to document the change with full patient attribution.`,
      patient_rights: [
        "This change was initiated and approved by the patient",
        "The original source data is preserved in the audit trail",
        "A Provenance record links this fix to the patient's intent",
        "The patient can request their provider correct the source record",
      ],
    };

    return result;
  }

  // --- Real-world action tools ---

  private async proposeAction(
    kind: string,
    payload: Record<string, unknown>,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const root = this.serverRoot();
    const resp = await fetch(`${root}/r6/actions/propose`, {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ kind, payload }),
    });
    if (!resp.ok) {
      let detail: unknown = null;
      try {
        detail = await resp.json();
      } catch {
        try { detail = await resp.text(); } catch { detail = null; }
      }
      return { error: `action_propose failed with status ${resp.status}`, detail };
    }
    return (await resp.json()) as Record<string, unknown>;
  }

  private async proposeRxTransfer(
    input: Record<string, unknown>,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const root = this.serverRoot();
    const body: Record<string, unknown> = {
      to_pharmacy: { name: input.to_pharmacy_name, phone: input.to_pharmacy_phone },
    };
    if (input.from_pharmacy_name || input.from_pharmacy_phone) {
      body.from_pharmacy = { name: input.from_pharmacy_name, phone: input.from_pharmacy_phone };
    }
    if (Array.isArray(input.medication_names) && input.medication_names.length) {
      body.medication_names = input.medication_names;
    }
    const resp = await fetch(`${root}/r6/actions/rx-transfer/propose`, {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      let detail: unknown = null;
      try {
        detail = await resp.json();
      } catch {
        try { detail = await resp.text(); } catch { detail = null; }
      }
      return { error: `rx_transfer_request failed with status ${resp.status}`, detail };
    }
    return (await resp.json()) as Record<string, unknown>;
  }

  private async commitAction(
    actionId: string,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const root = this.serverRoot();
    const resp = await fetch(`${root}/r6/actions/${encodeURIComponent(actionId)}/commit`, {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json", "X-Human-Confirmed": "true" },
    });
    if (!resp.ok) {
      let detail: unknown = null;
      try {
        detail = await resp.json();
      } catch {
        try { detail = await resp.text(); } catch { detail = null; }
      }
      const result: Record<string, unknown> = { error: `action_commit failed with status ${resp.status}`, detail };
      if (resp.status === 401) {
        result.requires_step_up = true;
      }
      return result;
    }
    return (await resp.json()) as Record<string, unknown>;
  }

  private async getActionStatus(
    actionId: string,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const root = this.serverRoot();
    const resp = await fetch(`${root}/r6/actions/${encodeURIComponent(actionId)}`, {
      headers,
    });
    if (!resp.ok) {
      let detail: unknown = null;
      try {
        detail = await resp.json();
      } catch {
        try { detail = await resp.text(); } catch { detail = null; }
      }
      return { error: `action_status failed with status ${resp.status}`, detail };
    }
    return (await resp.json()) as Record<string, unknown>;
  }

  // --- SMART Health Links (SHL) ---

  private async generateShl(
    input: Record<string, unknown>,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const root = this.serverRoot();
    const profile = (input.profile as string | undefined) || "intake";
    const patientId = input.patient_id as string | undefined;
    const rawDays = typeof input.expires_in_days === "number" ? input.expires_in_days : 7;
    const days = Math.min(Math.max(1, Math.round(rawDays)), 90);
    const label = typeof input.label === "string" ? input.label.slice(0, 80) : undefined;

    // Step 1: Fetch the guardrailed share-bundle from Flask
    const bundleResp = await fetch(`${root}/r6/fhir/$share-bundle`, {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({
        ...(profile !== "intake" ? { profile } : {}),
        ...(patientId ? { patient_id: patientId } : {}),
      }),
    });

    if (!bundleResp.ok) {
      let detail: unknown = null;
      try { detail = await bundleResp.json(); } catch {
        try { detail = await bundleResp.text(); } catch { detail = null; }
      }
      const result: Record<string, unknown> = {
        error: `share-bundle fetch failed with status ${bundleResp.status}`,
        detail,
      };
      if (bundleResp.status === 401) result.requires_step_up = true;
      return result;
    }

    const bundle = (await bundleResp.json()) as Record<string, unknown>;
    const resourceCount = (bundle.entry as unknown[] | undefined)?.length ?? 0;

    // Step 2: Simulation mode — SHL_SERVER_URL not configured
    const SHL_BASE = process.env.SHL_SERVER_URL;
    if (!SHL_BASE) {
      return {
        simulated: true,
        shlink: "shlink:/SIMULATED",
        note: `SHL_SERVER_URL not configured — returned stub. Bundle contained ${resourceCount} resources.`,
        resource_count: resourceCount,
      };
    }

    // Step 3: Generate master secret, derive auth + key
    const M = generateMasterSecret();
    const auth = await deriveAuth(M);
    const key = await deriveKey(M);
    const nowSeconds = Math.floor(Date.now() / 1000);
    const exp = nowSeconds + days * 86400;

    // Step 4: Create the SHL link on the server
    const createLinkResp = await fetch(`${SHL_BASE}/api/links`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${auth}`,
      },
      body: JSON.stringify({ flag: "U", exp }),
    });

    if (!createLinkResp.ok) {
      let detail: unknown = null;
      try { detail = await createLinkResp.json(); } catch {
        try { detail = await createLinkResp.text(); } catch { detail = null; }
      }
      return { error: `SHL /api/links failed with status ${createLinkResp.status}`, detail };
    }

    const linkData = (await createLinkResp.json()) as { id: string; url: string };

    // Step 5: Encrypt the bundle and upload ciphertext
    const jwe = await encryptJWE(
      utf8(JSON.stringify(bundle)),
      key,
      { cty: "application/fhir+json", deflate: true }
    );

    const uploadResp = await fetch(`${SHL_BASE}/api/manage/files`, {
      method: "POST",
      headers: {
        "Content-Type": "application/jose",
        "Authorization": `Bearer ${auth}`,
      },
      body: jwe,
    });

    if (!uploadResp.ok) {
      let detail: unknown = null;
      try { detail = await uploadResp.json(); } catch {
        try { detail = await uploadResp.text(); } catch { detail = null; }
      }
      return { error: `SHL /api/manage/files failed with status ${uploadResp.status}`, detail };
    }

    // Step 6: Build the shlink URI
    const shlink = buildShlink({
      url: linkData.url,
      key,
      exp,
      flag: "U",
      ...(label ? { label } : {}),
      v: 1,
    });

    // Step 7: Return result — NEVER log or persist M, key, or auth
    const expiresAt = new Date(exp * 1000).toISOString();
    return {
      shlink,
      viewer_link: buildViewerLink(SHL_BASE, shlink),
      manage_link: buildOwnerLink(SHL_BASE, M),
      expires_at: expiresAt,
      resource_count: resourceCount,
      _mcp_summary: `SMART Health Link created (expires ${expiresAt}). Give the manage link ONLY to the patient.`,
    };
  }

  // --- ChatGPT-connector-compatible search / fetch ---

  /**
   * Short human-readable summary for a resource, used as the `title` field
   * in connector search/fetch responses. Patient uses name.text (or
   * given+family); everything else prefers code.text, then
   * code.coding[0].display, then falls back to the resource id.
   */
  private summarizeResource(resourceType: string, resource: Record<string, unknown>): string {
    let display: string | undefined;

    if (resourceType === "Patient") {
      const name = ((resource.name as Array<Record<string, unknown>>) || [])[0];
      if (name) {
        const text = name.text as string | undefined;
        const given = (name.given as string[] | undefined) || [];
        const family = name.family as string | undefined;
        display = text || [...given, family].filter(Boolean).join(" ") || undefined;
      }
    } else {
      const code = resource.code as Record<string, unknown> | undefined;
      const codings = (code?.coding as Array<Record<string, unknown>> | undefined) || [];
      display = (code?.text as string | undefined) || (codings[0]?.display as string | undefined);
    }

    if (!display) display = (resource.id as string | undefined) || "unknown";
    return `${resourceType}: ${display}`;
  }

  /**
   * ChatGPT-connector `search` tool. Thin wrapper over the same Flask search
   * endpoint fhir_search uses — guardrails (PHI redaction, audit) are
   * inherited server-side. Query is a FHIR search string, e.g.
   * 'Observation?code=4548-4' or a bare 'Patient'.
   */
  private async connectorSearch(
    query: string,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    if (!query || typeof query !== "string") {
      return { error: "query is required" };
    }

    const qIdx = query.indexOf("?");
    const resourceType = qIdx === -1 ? query : query.slice(0, qIdx);
    const rawParams = qIdx === -1 ? "" : query.slice(qIdx + 1);
    if (!resourceType) {
      return { error: "query must start with a FHIR resource type" };
    }

    const params = new URLSearchParams(rawParams);
    const requestedCount = parseInt(params.get("_count") || "20", 10);
    const clampedCount = Math.min(
      Number.isFinite(requestedCount) && requestedCount > 0 ? requestedCount : 20,
      MAX_RESULT_ENTRIES
    );
    params.set("_count", clampedCount.toString());

    const resp = await fetch(
      `${this.baseUrl}/${encodeURIComponent(resourceType)}?${params.toString()}`,
      { headers }
    );
    if (!resp.ok) {
      return { error: `search failed with status ${resp.status}` };
    }

    const bundle = (await resp.json()) as Record<string, unknown>;
    const entries = (bundle.entry as Array<Record<string, unknown>> | undefined) || [];
    const results = entries.map((entry) => {
      const resource = (entry.resource as Record<string, unknown>) || {};
      return {
        id: `${resourceType}/${resource.id}`,
        title: this.summarizeResource(resourceType, resource),
        url: `${this.baseUrl}/${resourceType}/${resource.id}`,
      };
    });

    return { results };
  }

  /**
   * ChatGPT-connector `fetch` tool. Thin wrapper over the same Flask read
   * endpoint fhir_read uses — guardrails (PHI redaction, audit) are
   * inherited server-side. `id` must be 'ResourceType/id', as returned by
   * connectorSearch.
   */
  private async connectorFetch(
    id: string,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    if (!id || typeof id !== "string") {
      return { error: "id is required" };
    }
    const slashIdx = id.indexOf("/");
    const resourceType = slashIdx === -1 ? "" : id.slice(0, slashIdx);
    const resourceId = slashIdx === -1 ? "" : id.slice(slashIdx + 1);
    if (!resourceType || !resourceId) {
      return { error: "id must be in 'ResourceType/id' format" };
    }

    const resp = await fetch(
      `${this.baseUrl}/${encodeURIComponent(resourceType)}/${encodeURIComponent(resourceId)}`,
      { headers }
    );
    if (!resp.ok) {
      return { error: `fetch failed with status ${resp.status}` };
    }

    const resource = (await resp.json()) as Record<string, unknown>;
    const meta = (resource.meta as Record<string, unknown> | undefined) || {};

    return {
      id,
      title: this.summarizeResource(resourceType, resource),
      text: JSON.stringify(resource),
      url: `${this.baseUrl}/${resourceType}/${resourceId}`,
      metadata: {
        resourceType,
        lastUpdated: meta.lastUpdated as string | undefined,
      },
    };
  }
}
