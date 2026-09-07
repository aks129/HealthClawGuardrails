# Quickstarts — use HealthClaw from your AI agent

HealthClaw Guardrails is a remote MCP server. Any agent that speaks MCP can
connect to it and work with health records behind enforced guardrails (PHI
redaction, audit trail, human-in-the-loop, disclaimers).

## The connector URL

There is exactly one URL to paste. This is it:

```text
https://mcp-demo-production-ee2c.up.railway.app/mcp
```

No API key. No Client ID. If your agent asks for either one, you have the wrong
URL: see [Troubleshooting](claude.md#troubleshooting).

The server is hard-pinned to the `desktop-demo` tenant, a synthetic patient
panel with conditions, labs, immunizations, and medications. **None of it is
real patient data, so it is safe to record.**

### There is a second server, and you do not want it

Real records live on a separate production endpoint, `mcp-server-production-5112`.
It requires a deployment-scoped bearer token and answers 401 without one. Hosted
connectors cannot attach that header, so pasting it produces a sign-in loop that
cannot succeed. The full URL is deliberately not printed here, because the only
reason to copy it from this page would be by mistake. See
[mcp-generic.md](mcp-generic.md#tenancy-and-auth) for the tenancy model.

## Pick your agent

| Agent | Guide | Works on phone? |
| --- | --- | --- |
| Claude (claude.ai, desktop, iOS/Android) | [claude.md](claude.md) | Yes — add the connector on the web once, it appears in the mobile app |
| Perplexity (Pro/Max) | [perplexity.md](perplexity.md) | Connector added in settings; available wherever you use Perplexity |
| ChatGPT (Plus/Pro, Developer Mode) | [chatgpt.md](chatgpt.md) | Dev-mode connectors work in the ChatGPT apps |
| Telegram (OpenClaw bot) | [telegram.md](telegram.md) | Yes — pure chat |
| Claude Code / any MCP client | [mcp-generic.md](mcp-generic.md) | — |

WhatsApp and iMessage are not supported yet (no MCP surface there today);
Telegram is the chat-app path.

## The 10-minute demo script (works in any connected agent)

Say these to your agent, in order. Each one exercises a different part of the
stack. Everything runs against the synthetic demo tenant. It contains several
patients. Select one before asking clinical questions, and keep that patient
throughout the walkthrough. Ten minutes is a planning estimate, not a measured
setup time for every client.

1. **Confirm the connection**
   > What HealthClaw tools do you have available? List them by group.

2. **Choose a synthetic patient, then summarize their records**
   > Use fhir_search with resource_type Patient. Show the returned patient
   > references and ask me which one to use. Do not combine their records.

   Choose a returned `Patient/<id>`. Then say:

   > Use that patient for the rest of this demo. Search their conditions,
   > observations, and medications with the patient filter. Summarize only
   > those records and say what you could not establish.

3. **Lab interpretation** (decision support, never diagnosis)
   > Call fhir_interpret_labs with subject set to the selected Patient
   > reference. Explain the recent lab results in plain language.

4. **Preventive care gaps** — the "what am I due for?" question
   > Call care_gaps with subject set to the same Patient reference. Explain
   > what may be due and what could not be checked.

   If the result says `ambiguous-patient`, no patient was evaluated. Repeat
   the call with the selected reference. An empty result is not evidence that
   nothing is overdue.

5. **Data quality (Curatr)**
   > Run Curatr on an observation or condition returned for the selected
   > patient. Use its returned resource type and id. Any coding problems?

6. **Next-steps synthesis**
   > Based on everything you found — the labs, the care gaps, the data
   > quality — what are the recommended next steps? Note what needs a
   > clinician.

7. **Inspect the guardrail self-test**
   > Run the guardrail conformance check and show me the grade.

   The scorecard measures redaction, audit, step-up, the confirmation gate,
   tenant isolation, disclaimers, and error fidelity on synthetic data.
   Read its scope notes. It is not a clinical assessment or an independent
   security audit. The direct-write confirmation check does not establish
   human attestation, as its report explains.

8. **Show that writes are gated**
   > Call fhir_commit_write to create a synthetic observation for the selected
   > patient. Do not supply credentials. Show the refusal without retrying.

   The keyless demo refuses this call with "Step-up authorization required".
   The JSON-RPC bridge returns that refusal inside `result`, even with HTTP
   200. Inspect the body. This proves a missing-credential refusal, not the
   separate human approval and execution journey.

9. **Inspect the share-link gate** (SMART Health Links)
   > Call shl_generate without credentials. Show its refusal without retrying.

   Sharing also requires step-up authorization. The keyless demo returns
   "Step-up authorization required", not a working link. Creating a link is
   a separate authorized workflow.

## Connecting your own health data (Fasten Connect)

**The demo connector cannot read your private records.** It ignores supplied
tenant context and stays on synthetic data. Pasting a private tenant or token
into chat does not change that. Do not paste credentials into the demo chat.

Real-record access needs a separately configured path:

- **CareAgents:** follow the [beta tester guide](../beta-tester-guide.md).
  Real-record connections are closed for the current synthetic cohort.
  If your account has separately authorized access, use the connection flow
  offered in its hub. A "coming soon" tile does not start a connection.
- **A local MCP client:** an operator must configure the production endpoint,
  its deployment bearer credential, and the tenant-bound read credential.
  See [tenancy and auth](mcp-generic.md#tenancy-and-auth). Credentials belong
  in the client's protected configuration, not in a conversation.

Hosted connector access to private records remains pending
[#290](https://github.com/aks129/HealthClawGuardrails/issues/290). A successful
demo connection does not prove that private-record access is configured.

**If you are recording videos: stay on the synthetic demo tenant.** Never
film real PHI, including your own.
