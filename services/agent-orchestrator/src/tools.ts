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
 *   curatr.evaluate
 * - Write (require step-up): fhir.propose_write, fhir.commit_write,
 *   curatr.apply_fix
 *
 * All tools include MCP annotations (readOnlyHint, destructiveHint, openWorldHint).
 */

import fetch from "node-fetch";

export type ToolTier = "read" | "write";

interface ToolAnnotations {
  readOnlyHint: boolean;
  destructiveHint: boolean;
  openWorldHint: boolean;
}

interface ToolDefinition {
  name: string;
  description: string;
  tier: ToolTier;
  annotations: ToolAnnotations;
  inputSchema: Record<string, unknown>;
}

// MCP SDK tool schema format (includes annotations)
export interface MCPToolSchema {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  annotations: ToolAnnotations;
}

// Cap search results for token safety (marketplace limit: <25k tokens)
const MAX_RESULT_ENTRIES = 50;

export class FHIRTools {
  private baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  /**
   * Return tool schemas in MCP SDK format (for ListToolsRequestSchema handler).
   * Includes annotations required by OpenAI and Anthropic marketplaces.
   */
  getMCPToolSchemas(): MCPToolSchema[] {
    return this.getToolSchemas().map((t) => ({
      name: t.name,
      description: t.description,
      inputSchema: t.inputSchema,
      annotations: t.annotations,
    }));
  }

  getToolSchemas(): ToolDefinition[] {
    return [
      {
        name: "context_get",
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
        name: "fhir_propose_write",
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
        name: "fhir_lastn",
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
      // --- Curatr: patient-facing data quality tools ---
      {
        name: "curatr_evaluate",
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

    // Enforce step-up for commit_write
    if (tool.tier === "write" && toolName === "fhir_commit_write") {
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
    const fwdHeaders: Record<string, string> = {
      "Content-Type": "application/fhir+json",
    };
    if (headers?.["x-tenant-id"]) fwdHeaders["X-Tenant-Id"] = headers["x-tenant-id"];
    if (headers?.["x-step-up-token"]) fwdHeaders["X-Step-Up-Token"] = headers["x-step-up-token"];
    if (headers?.["x-agent-id"]) fwdHeaders["X-Agent-Id"] = headers["x-agent-id"];
    if (headers?.["authorization"]) fwdHeaders["Authorization"] = headers["authorization"];

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

  // --- Curatr: patient-facing data quality tools ---

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
}
