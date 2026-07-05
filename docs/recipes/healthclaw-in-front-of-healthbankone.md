# Recipe: HealthClaw in front of Health Bank One

**The pairing:** Health Bank One provides **identity-verified, structured, clinically-coded**
health records over MCP. HealthClaw wraps that data in the guardrail layer every
agent needs — PHI redaction, immutable audit, step-up authorization,
human-in-the-loop, tenant isolation — and then runs **clinical intelligence** on
top of the codes HBO now emits. HBO owns the trust/identity rail; HealthClaw owns
the safety + intelligence rail. Together: verifiable, scoped, safe agent access to
real records.

```text
Health Bank One          HealthClaw Guardrails                     Any agent
(identity-verified   ─▶  redact · audit · step-up · HITL · tenant  ─▶  Claude /
 MCP: structured         + clinical intelligence on the codes          OpenAI /
 results + codes)          ($interpret, $evaluate-measure, Curatr)     Gemini
```

## Why HBO's structured-codes upgrade matters

HBO now returns **structured results with clinical codes** in its MCP payload
(LOINC on labs, coded conditions, coded meds). That is exactly the input
HealthClaw's clinical engines need — so the moment HBO data lands behind the
guardrails, three capabilities light up with no extra mapping:

| HBO structured data | HealthClaw runs | Output |
| --- | --- | --- |
| LOINC-coded lab Observations | `POST /r6/fhir/Observation/$interpret` | flags low/normal/high/critical + a plain-language consumer summary, decision-support (not diagnosis) |
| Coded Conditions + BP Observations | `POST /r6/fhir/Measure/nqf0018-controlling-high-bp/$evaluate-measure` | a FHIR MeasureReport — blood-pressure control rate |
| Any coded resource | `curatr_evaluate` | validates the codes against public terminology (LOINC/SNOMED/RxNorm) |

Coded data in → clinical value out. Unstructured data (notes, documents) is a
separate rail: **redacted by default** (free-text is where PHI hides and where
LLMs hallucinate), surfaced only on explicit intent and always with Provenance.

## Wiring it

HealthClaw already pulls from HBO's MCP server and redacts in-process before
anything touches disk:

```bash
export HBO_MCP_URL=https://<hbo-host>/mcp
export HBO_ACCESS_TOKEN=<token>          # or: python scripts/healthbankone_oauth.py authorize
python scripts/export_healthbankone_mcp.py --tenant-id <tenant> --discover
```

`--discover` reads HBO's `tools/list` at runtime (nothing hardcoded), calls the
read-safe tools, redacts every record through the HealthClaw rules, and ingests
into the guardrailed store. From there every agent surface — MCP, the OpenAI /
Gemini adapters, the Claude connector — sees the same redacted, audited, coded data.

## Prove the guardrails hold — on HBO data

The guardrails aren't a claim; they're a scorecard. Point the conformance harness
at the HBO-backed deployment:

```bash
python scripts/guardrail_conformance.py --base-url https://<deployment> \
  --tenant <hbo-tenant> --step-up-token <token>
# → Grade A (6/6): PHI Redaction · Audit · Step-Up · Human-in-the-Loop · Tenant Isolation · Disclaimers
```

A partner (or a regulator) can run it and see, on synthetic data, that every
guardrail fires regardless of the backend.

## Identity: OAuth/OIDC scope authorization (roadmap)

HBO's OAuth/OIDC service — SSO, passwordless, Open-Banking-grade — is the trust
layer HealthClaw's authorization model wants. Today HealthClaw gates writes with
HMAC step-up tokens and tenant-bound reads. With HBO OAuth, an agent's **scope**
is authorized by a certificate a third party can cryptographically verify: the
guardrails stop enforcing self-asserted tenancy and start enforcing
externally-verifiable, scoped consent. That is the Open-Banking pattern applied
to health data — HBO issues the scoped, verifiable credential; HealthClaw
enforces it at every tool call and audits the result.

## Status

- **Working today:** MCP pull + in-process redaction (`export_healthbankone_mcp.py`),
  OAuth PKCE + Railway callback broker (`healthbankone_oauth.py`, `/shc/hbo/*`),
  clinical intelligence over coded data, conformance scorecard.
- **In flight (HBO, next week):** OAuth/OIDC scope authorization → HealthClaw
  agent-scope enforcement.
- **Joint deliverable:** this recipe, demoed end-to-end on a call.
