# Roadmap

The working plan, in the open. The **issue tracker is the canonical log** —
everything here links to a tracked issue where one exists. Contributions
welcome on all of it: several items are labeled
[`good first issue`](https://github.com/aks129/HealthClawGuardrails/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).

## Near term (v1.7)

**Clinical intelligence**
- Lab interpreter: broader analyte table — TSH, liver panel, vitamin D ([#53](https://github.com/aks129/HealthClawGuardrails/issues/53))
- Lab interpreter: unit conversion (mg/dL ↔ mmol/L) with cited factors ([#54](https://github.com/aks129/HealthClawGuardrails/issues/54))
- Lab interpreter: trend/delta interpretation across successive results ([#62](https://github.com/aks129/HealthClawGuardrails/issues/62))
- Quality measures: complete the CMS165 denominator exclusion set ([#55](https://github.com/aks129/HealthClawGuardrails/issues/55)); current-year default period ([#52](https://github.com/aks129/HealthClawGuardrails/issues/52))
- Clinical review of the `LOINC_RANGES` reference table before it is presented
  as authoritative in live demos
- Care-gaps interpreter (`Patient/$care-gaps`): preventive-care reminders from a
  patient's own record — sourced USPSTF/ACIP/ADA rules, conservative
  `indeterminate` handling. Follow-ups: risk-adjusted cadence, condition-driven
  rules beyond diabetes A1c

**Demos & workflows**
- SMBP phase 2: BP-cuff photo OCR intake; wire the reminder scheduler to a
  send cadence ([#61](https://github.com/aks129/HealthClawGuardrails/issues/61))
- SMART Health Links: adopt upstream QR rendering + revocation

**Engineering health**
- Carve `r6/routes.py` into the established `register_*` module pattern ([#56](https://github.com/aks129/HealthClawGuardrails/issues/56))
- Split the MCP server's `tools.ts` into definitions + executors ([#57](https://github.com/aks129/HealthClawGuardrails/issues/57))
- Jest teardown open-handle fix ([#58](https://github.com/aks129/HealthClawGuardrails/issues/58))

## Ecosystem & interoperability

- **Catalog presence:** official MCP Registry (✅ published), ClawHub skills
  (✅ published), Gemini CLI extension (✅ in-repo), Hermes `optional-mcps`
  catalog (PR open), skill discovery at
  `/.well-known/agent-skills/index.json` (✅ live)
- **Upstream contributions:** fixes and implementer feedback to Medplum,
  SMART Health Links tooling, open-wearables, HL7 SDC
  ([FHIR-57806](https://jira.hl7.org/browse/FHIR-57806)), and the Tuva
  Project's quality-measure mart
- **Partner integrations:** deepen the Health Bank One pairing (structured
  coded records + OAuth scope authorization — see
  [the design doc](design/oauth-scope-mapping-hbo.md)) and the
  Medplum-in-front recipe
- **CMS-0057-F prior-auth (2027 mandate):** position the existing SDC
  `$populate`/`$extract` engine as the guardrailed documentation layer for
  Da Vinci DTR — redaction on egress, audit, human-in-the-loop, verifiable
  `$conformance`. Scope, gaps (CQL bridge), and phasing in
  [the design doc](design/cms-0057-prior-auth-dtr.md)

## Honesty ledger (deliberate scope limits)

These are documented limits, not oversights:

- The NQF 0018 measure is a **calculator, not a certified eCQM** (partial
  exclusions; see #55)
- Lab interpretation is **decision support, never diagnosis**; adult
  population defaults only (no pediatric/pregnancy ranges yet)
- Claims/coverage data is stored and audited but **not analyzed** (no
  cost/denial analytics)
- Validation is structural, not full StructureDefinition/terminology
  conformance

## How to influence this

Open an issue, comment on an existing one, or bring implementer experience —
the fastest-moving items are the ones users push on.
