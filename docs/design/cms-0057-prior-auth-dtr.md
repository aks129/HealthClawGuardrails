# Design: CMS-0057-F prior-auth support — HealthClaw as the DTR safety rail

**Status:** Proposal / discussion primer for the CMS Health Tech ecosystem.
**Goal:** position HealthClaw's *existing* SDC engine (`$populate`/`$extract`) as the
guardrailed automation layer for the documentation step of electronic prior
authorization (ePA) — the piece the 2027 mandate makes unavoidable and that
patient/provider AI agents will increasingly try to fill.

## Why now

The **CMS Interoperability and Prior Authorization Final Rule (CMS-0057-F)**
requires impacted payers (MA, Medicaid/CHIP, QHPs on the FFEs) to stand up
FHIR-based prior-authorization APIs, with the operational provisions effective
**January 1, 2027**. The Da Vinci implementation stack for that mandate is:

| Rule surface | Da Vinci IG | What it needs |
| --- | --- | --- |
| "Is PA required, and what are the rules?" | **CRD** (Coverage Requirements Discovery) | CDS Hooks |
| "Collect the required documentation" | **DTR** (Documentation Templates & Rules) | **SDC Questionnaires + CQL** |
| "Submit and check status" | **PAS** (Prior Auth Support) | X12 278 ↔ FHIR |

**DTR is a SMART app / CQL engine that renders a payer's Questionnaire, auto-fills
it from the EHR/FHIR record, and produces a QuestionnaireResponse.** That is
structurally the operation HealthClaw already ships:

- `POST /r6/fhir/Questionnaire/$populate` — pre-fills a Questionnaire from the
  subject's record (expression-based `initialExpression` FHIRPath +
  observation-based LOINC matching).
- `POST /r6/fhir/QuestionnaireResponse/$extract` — turns the completed response
  into a committed transaction Bundle, step-up + `$validate` gated.

We are **not** claiming to be a certified DTR app. We are claiming that the
population/extraction *mechanics* — plus a guardrail layer no reference DTR app
has — are already here and testable.

## The wedge: agents will do this whether or not it's safe

The mandate creates FHIR PA APIs; the market fills the documentation step with
automation. A patient- or provider-side AI agent asked to "start my prior auth"
will read the clinical record and populate the payer's form. That is:

- a **PHI-egress** step (record → form → payer),
- an **assertion-of-fact** step (the agent is attesting clinical documentation),
- an **irreversible-submission** step once it reaches PAS.

Exactly the three places HealthClaw's guardrails exist to gate. The pitch to the
ecosystem isn't "we built DTR." It's **"if an agent is going to fill prior-auth
forms, the fill step should be redacted, audited, human-confirmed, and
conformance-verifiable — here's a working reference for that."**

## What maps today vs. what's a gap

**Already real (v1.6):**
- Questionnaire populate/extract round-trip, pure engines in `r6/sdc/`.
- Guardrails on both: tenant-read auth + AuditEvent on `$populate`; step-up +
  per-resource `$validate` + human-in-the-loop posture on `$extract` commit.
- Provenance: every access emits an immutable AuditEvent; `$conformance`
  self-test proves the guardrails are live.

**Gaps to be honest about (tracked, not hand-waved):**
1. **CQL.** DTR rules ship as CQL logic bound to the Questionnaire. HealthClaw's
   populate is FHIRPath/LOINC-expression-based, **not a CQL engine**. Bridging
   means either embedding a CQL evaluator (e.g. via a CQL-to-ELM service) or
   scoping to `initialExpression`-only payer templates. This is the load-bearing
   gap — name it first in any CMS conversation.
2. **CRD / CDS Hooks.** No hook surface today. Out of scope for v1; DTR can be
   launched standalone per the IG.
3. **PAS / X12 278.** The actual submission rail is not ours and shouldn't be —
   HealthClaw sits *in front of* PAS as the documentation-integrity layer, not as
   the clearinghouse.
4. **DTR conformance.** We have not run the Da Vinci DTR test kit / Touchstone.
   Until we do, the language is "SDC-compatible populate/extract," never
   "DTR-conformant."

## Target shape (proposal, phased)

```text
Payer Questionnaire (+ CQL rules)
        │
        ▼
$populate ──▶ auto-filled QuestionnaireResponse ──▶ [human review] ──▶ $extract
   │  guardrails:                                          │            │
   │   • pull only in-scope record elements                │            │ step-up +
   │   • AuditEvent on every read                          │            │ $validate +
   │   • redaction profile for anything leaving the tenant │            │ human-confirmed
   └──────────────────────────────────────────────────────┘            ▼
                                                              committed Bundle → (PAS, not ours)
```

- **Phase 1 (bounded, buildable now):** accept a payer DTR Questionnaire whose
  prepopulation is `initialExpression`-expressible; run it through the existing
  `$populate`/`$extract` with a **`prior-auth` redaction/annotation profile** and
  a Provenance stamp on the response. Demo against a public sample payer template.
- **Phase 2:** CQL evaluation for `Questionnaire.item` populate context — the
  real DTR bridge. Spike a CQL-to-ELM path; gate scope to what we can actually
  execute and *say so* in the CapabilityStatement.
- **Phase 3:** Da Vinci DTR test-kit run; only then adopt DTR-conformance
  language.

## Why this is the right lane for HealthClaw

Reference DTR apps prove the *workflow*. None of them are built to prove the
*safety of an autonomous agent* driving that workflow — redaction on egress,
immutable audit, human-in-the-loop on the attesting write, and a machine-checkable
`$conformance` grade. That's the differentiator, and it's the same "verifiable
safety rail" story as the HBO pairing: **someone else owns the mandate rail; we
own the proof that the automation touching it stayed inside the guardrails.**

## Honesty guardrails for any external framing

- Say "SDC populate/extract that *supports* the DTR documentation step," never
  "DTR." No conformance claim without a test-kit run.
- Name the CQL gap unprompted — it's the first question any Da Vinci implementer
  will ask.
- The value proposition is the guardrail layer, not novel PA plumbing. Don't
  oversell coverage of CRD/PAS we don't have.

## References

- CMS-0057-F Final Rule (Interoperability and Prior Authorization), operational
  provisions effective 2027-01-01.
- Da Vinci **DTR** (Documentation Templates and Rules) IG — SDC + CQL.
- Da Vinci **CRD**, **PAS** IGs — adjacent surfaces, out of scope here.
- HL7 **Structured Data Capture (SDC)** IG — `$populate` / `$extract` operations.
- In-repo: `r6/sdc/` (engines), `r6/sdc/routes.py` (guardrailed handlers),
  CLAUDE.md "SDC Forms" section (compliance postures H2/H4).
