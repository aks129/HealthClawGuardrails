# Quickstart patient selection — measured demo behavior

Measured 2026-09-06 against source `957847f` and the public synthetic MCP demo.
The deployed MCP health response reported version `1.9.0`.

## Outcome

The demo contains four patients. A care-gap request without a subject refuses
to select one. Supplying a returned Patient reference evaluates seven rules.
The quickstart now asks the reader to select a patient before clinical calls.

The private-record section also stops instructing readers to paste credentials
into this demo connection. The demo endpoint remains pinned to synthetic data.
This documentation change does not add private-record connector support.

## Calls executed

Transport: the documented JSON-RPC bridge at
`https://mcp-demo-production-ee2c.up.railway.app/mcp/rpc`.
No authentication credential was supplied.

| Call | Input | Observed result |
| --- | --- | --- |
| `tools/list` | No arguments | 27 tool definitions |
| `fhir_search` | Patient, count 5 | Four Patient entries |
| `fhir_validate` | Minimal synthetic Patient | OperationOutcome warning about missing name or identifier |
| `fhir_interpret_labs` | Synthetic cholesterol observation, value 180, supplied range 0–200 | One normal interpretation, with a consumer summary and disclaimer |
| `care_gaps` | No subject | `ambiguous-patient`, zero rules evaluated |
| `search` | Observation | Nonempty search results |
| `care_gaps` | Patient reference selected from the search result | Seven rules evaluated |
| `fhir_commit_write` | Synthetic Observation create, no credential | `result.error` is "Step-up authorization required", `requires_step_up` is true |
| `shl_generate` | No credential | Same step-up refusal, no share link |

The result bodies were inspected and asserted after capture. HTTP 200 alone
was not the success criterion. The bridge returns domain objects in `result`.
It does not use the standard MCP content-block envelope for these responses.

## What this establishes

- The patient references needed by the guide can be obtained from the demo.
- The missing-subject case is an explicit refusal to choose a patient.
- Selecting a returned subject reaches the care-gap evaluator.
- Five distinct read tools and two missing-credential refusals were exercised.
  Listing 27 tools does not exercise 27 tools.

The first refusal probe incorrectly expected a top-level JSON-RPC `error`.
It failed on the bridge's actual `result.error` envelope. The corrected probe
asserted both the refusal message and `requires_step_up`, and both calls passed.

## Limits and remaining work

- No hosted-client installation or mobile setup duration was measured.
- No claim is made that every agent follows the written prompts correctly.
- The lab input was synthetic. The result is not a clinical sign-off.
- No signed-in CareAgents journey or connector tile was exercised in this run.
- No private tenant token was supplied to the demo to test its isolation.
  The tenancy distinction follows the existing transport contract in
  [the generic quickstart](../quickstarts/mcp-generic.md#tenancy-and-auth).
- No form was submitted, share link created, or external action executed.
  The refusal responses were verified. Persistence was not independently queried.
- No recording or independent end-user sign-off exists for this change yet.

The broader clinician acceptance checklist is
[#677](https://github.com/aks129/HealthClawGuardrails/issues/677).
Coordination is tracked in
[#676](https://github.com/aks129/HealthClawGuardrails/issues/676).
