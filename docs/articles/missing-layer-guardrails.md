# The Missing Layer: Why AI Agents Need Guardrails Before They Get FHIR Access

FHIR standardized how health data is structured. MCP standardized how AI connects to tools. Nobody standardized the guardrails in between.

That's the gap. And it's getting wider every week.

---

## The convergence nobody planned for

Two things happened in parallel over the past eighteen months that are about to collide in healthcare IT.

First, Model Context Protocol went from Anthropic side project to industry standard. OpenAI, Google, Microsoft, AWS — everyone adopted it. The Linux Foundation took over governance. MCP gave AI agents a universal way to call tools, and the ecosystem responded. Thousands of MCP servers spun up almost overnight.

Second, FHIR kept maturing. CMS mandated FHIR-based APIs for prior authorization by January 2026. FHIR R6 is tracking for late 2026 with most clinical and administrative resources going normative. ONC is expected to push FHIR Subscriptions, Bulk FHIR SLAs, and — critically — FHIR Create/Write into HTI-6. For the first time, the regulatory posture isn't just "let apps read data." It's "let systems write it back."

Now combine those two trends. An AI agent, connected via MCP, talking to a FHIR server, with write access. That's not a hypothetical. AWS shipped a HealthLake MCP server with 11 tools. Momentum built one. WSO2 built one. There's already an arxiv paper on an MCP-FHIR framework for dynamic EHR extraction.

Every one of these projects solves the plumbing problem. None of them solve the trust problem.

## What happens when an agent reads a patient record?

Here's what a raw MCP-to-FHIR integration gives you: the agent asks for a Patient resource, it gets back a Patient resource. Full name, full date of birth, full address, every identifier, every telecom number. The agent now has PHI in its context window.

Was that access logged? In an immutable, HIPAA-ready audit trail, or in whatever logging the FHIR server happens to do? Was the agent scoped to a single tenant, or could it cross-query? If the agent decides to write an Observation based on what it read — a lab result, a vital sign, a clinical note — who confirmed that write was appropriate? The agent? The model? Nobody?

These aren't edge cases. They're the default behavior of every MCP-FHIR integration that ships without a guardrail layer.

Enkrypt AI published a case study quantifying this. Database-level security alone — parameterized queries, input validation — blocked 20% of attacks with zero protection against prompt injection or HIPAA violations. Comprehensive guardrails achieved 90% attack prevention with zero critical vulnerabilities. The gap between "we have security" and "we have guardrails" is enormous.

## The OpenClaw lesson

OpenClaw went from zero to 217,000 GitHub stars in under three months. 196,000+ stars, 600+ contributors, 5,700+ skills on ClawHub. That's the fastest adoption curve in the AI tooling space this year, and it demonstrated something important: when you give developers a way to distribute agent capabilities as composable units, they will build at a pace that outstrips any review process.

It also demonstrated the risks. A security audit of ClawHub found that 12% of skills were malicious — 341 out of roughly 2,800 audited. Data exfiltration. Prompt injection payloads. 512 vulnerabilities total, 8 rated critical. A WebSocket origin bypass gave attackers remote code execution (CVE-2026-25253, CVSS 8.8). 135,000 exposed instances on the public internet, 50,000 directly vulnerable.

Now imagine that distribution model applied to healthcare. An agent skill that reads from Epic, writes to HAPI, and has no redaction, no audit trail, no step-up authorization. A skill that exposes PHI because the developer didn't know they needed to mask it. A skill that writes clinical data without human confirmation because the developer didn't know that was a requirement.

This isn't a supply chain attack. It's a supply chain omission. The tooling to prevent these problems doesn't exist in the skill itself because nobody built it into the protocol layer.

## What a guardrail layer actually looks like

We built one. It's open source, it's 254 tests, and it's a pattern library — not a product pitch. The question we started with: what if the security and compliance layer was a proxy that sat between *any* AI agent and *any* FHIR server, and enforced guardrails on every request regardless of what the agent or the skill developer remembered to do?

The architecture:

```
AI Agent → MCP Server → Guardrail Proxy → Any FHIR Server
                            ↓
                   PHI redaction
                   Immutable audit trail
                   Step-up authorization
                   Human-in-the-loop
                   Tenant isolation
                   URL rewriting
```

Every read path: PHI redacted before the agent sees it. Names become initials. Identifiers get masked. Addresses stripped. Birth dates truncated to year. Photos removed. The agent gets enough clinical context to be useful, not enough to create a breach.

Every write path: two-phase propose/commit. The agent proposes a write, the system validates it structurally, identifies whether it's a clinical resource type (Observation, Condition, MedicationRequest, etc.), and if so, requires a human-in-the-loop confirmation before the write lands. Step-up authorization via HMAC-SHA256 tokens with 5-minute TTL and tenant binding. You can't replay a token, you can't cross tenants, you can't skip the human gate.

Every request, period: tenant isolation at the database layer. Immutable audit trail, append-only, with database-level enforcement. Medical disclaimers injected on clinical resource reads.

When proxying to an upstream FHIR server — HAPI, SMART Health IT, Epic — the guardrails apply to upstream responses too. The upstream server's URLs get rewritten so they never leak to the client. The agent doesn't know it's talking to Epic. It just knows it's talking to a FHIR server with guardrails.

## Why this is an infrastructure problem, not a feature problem

Anthropic launched Claude for Healthcare at JPM in January. BAA available through AWS, GCP, and Azure. Skills for FHIR development, ICD-10 lookup, NPI verification, PubMed search, prior auth review. Banner Health deployed it internally across 33 hospitals with 80-85% of users reporting time savings.

That's all good. But the existing healthcare skills in the marketplace are knowledge skills — they teach Claude about FHIR structures, coding systems, validation patterns. They don't enforce anything at runtime. The `fhir-developer@healthcare` skill tells you how a Patient resource should look. It doesn't stop an agent from reading one without redacting PHI.

Our project provides 10 MCP tools — `fhir.read`, `fhir.search`, `fhir.validate`, `fhir.stats`, `fhir.lastn`, `fhir.permission_evaluate`, `fhir.subscription_topics`, `context.get`, `fhir.propose_write`, `fhir.commit_write` — and guardrails are enforced at the server layer, not the skill layer. You can't opt out. You can't forget to redact. You can't skip the audit trail.

We packaged it as a Claude Code plugin with three skills, an OpenClaw/ClawHub skill, and an MCP server manifest. Not because we think distribution alone solves the problem — OpenClaw proved it doesn't — but because the guardrails need to exist where the tools are. If someone installs a FHIR skill from ClawHub, the guardrail layer should be what they're installing, not something they have to remember to add later.

## What R6 brings to the table

FHIR R6 introduces the Permission resource — access control that's separate from Consent. We implemented `$evaluate`, which takes an actor, a purpose, and a target resource and returns a permit/deny decision with reasoning. The reasoning part matters. When an AI agent asks "can I access this patient's records for treatment purposes?" and the answer is "denied: no active Permission rule found for actor 'agent-1' on Patient/123," that's an auditable, explainable decision. Not a 403 with no context.

R6 also brings DeviceAlert (ISO/IEEE 11073 device alarms), NutritionIntake (dietary tracking), and a restructured SubscriptionTopic. We support all of them with CRUD and validation. SubscriptionTopics are stored and discoverable — notifications aren't dispatched yet, but the resource model is there for when HTI-6 makes push-based FHIR a regulatory requirement.

## The honest limitations

This is a pattern library, not a production FHIR server. Local mode stores resources as JSON blobs in SQLite. Validation is structural — required fields and value constraints, no StructureDefinition conformance, no terminology binding. Human-in-the-loop is a header flag, not a cryptographic confirmation protocol. The audit trail is immutable at the database layer but doesn't have external attestation.

The upstream proxy doesn't cache responses, doesn't do cross-version translation (R4 responses stay R4), and doesn't forward SMART-on-FHIR auth to the upstream server. Tenant isolation is enforced locally, not on the upstream.

We're calling it v0.9.0 and we mean it. The patterns are solid. The implementation is a reference, not a production deployment.

## The opportunity

There are maybe a dozen MCP-FHIR projects in the wild right now. Most of them will grow. Most of them will get deployed somewhere that matters. And most of them are shipping without a guardrail layer because nobody defined what that layer should look like.

The arxiv paper on HIPAA-compliant agentic AI recommends Attribute-Based Access Control, hybrid PHI sanitization combining regex and BERT, and immutable audit trails. The MIT field guide for deploying AI in clinical practice calls for continuous monitoring, red-teaming, and "nutrition labels" — if a vendor can't show what data they touch, whether PHI is retained, and what their failure modes are, the answer is "no label, no deployment."

All of that is correct. And all of it assumes someone builds the infrastructure to enforce it.

We think the right answer is a vendor-neutral proxy layer that works with any FHIR server and any MCP client. Not a feature in one vendor's product. Not a compliance checkbox. An actual enforcement layer with tests, with documentation about what it does and doesn't do, and with honest limitations listed where people can read them.

The code is at [github.com/aks129/ModelContextProtocolFHIR](https://github.com/aks129/ModelContextProtocolFHIR). It's MIT licensed. 254 tests passing. We're submitting it to the Anthropic healthcare marketplace and publishing it on ClawHub.

If you're building MCP-to-FHIR integrations, or thinking about deploying AI agents against clinical data, or just trying to figure out what guardrails should look like in this space — take a look. Open an issue. Tell us what we're missing. The gap between "agents can access FHIR data" and "agents can safely access FHIR data" is closing whether we're ready or not. Better to shape it than react to it.

---

*Eugene Vestel is the founder of FHIR IQ. This project is open source and is not affiliated with or endorsed by HL7, Anthropic, or any FHIR server vendor mentioned.*
