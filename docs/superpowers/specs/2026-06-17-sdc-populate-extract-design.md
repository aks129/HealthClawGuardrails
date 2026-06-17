# SDC Form Population & Data Extraction — Design

**Date:** 2026-06-17
**Status:** Approved (design); pending implementation plan
**Author:** Eugene Vestel + Claude Code
**Spec source:** HL7 SDC IG — [$populate](https://build.fhir.org/ig/HL7/sdc/en/populate.html), [data extraction](https://build.fhir.org/ig/HL7/sdc/en/extraction.html)

## Goal

Make healthclaw.io a skilled agent at filling out and extracting healthcare data
forms, using the standard HL7 **Structured Data Capture (SDC)** operations so the
result is interoperable with any SDC form-filler:

- **`$populate`** — given a `Questionnaire` + a `subject` + supporting `content`,
  return a pre-filled `QuestionnaireResponse`.
- **`$extract`** — given a completed `QuestionnaireResponse`, return a transaction
  `Bundle` of FHIR resources to create/update.

These are two halves of one capability (form round-trip) and ship together.

## Scope (v1)

Supported SDC mechanisms:

| Operation | Mechanism | Directive |
| --- | --- | --- |
| `$populate` | Observation-based | `item.code` (LOINC) + observation population |
| `$populate` | Expression-based | `initialExpression` (FHIRPath) |
| `$extract` | Observation-based | `observationExtract` extension + `item.code` |
| `$extract` | Definition-based | `definitionExtract` + `definition` + `definitionExtractValue` |

**Out of scope (v1):** StructureMap-based and CQL populate; template-based and
StructureMap-based extract. The extension-driven engine design leaves room to add
these later without reworking interfaces.

## Architecture

New module `r6/sdc/`, mirroring the `r6/actions/` and `r6/fasten/` pattern. The
transform **engines are pure** (resource in, resource out) so they unit-test
without Flask; all auth/audit/store/redaction I/O lives in the blueprint.

```text
r6/sdc/
  __init__.py
  expressions.py   FHIRPath wrapper (fhirpathpy) + launchContext / variable / context resolution
  populate.py      populate engine — (Questionnaire, subject, content) -> QuestionnaireResponse  [pure]
  extract.py       extract engine — (QuestionnaireResponse, Questionnaire) -> transaction Bundle [pure]
  routes.py        Flask blueprint: $populate / $extract; auth, audit, store I/O, redaction
```

Rationale for a separate blueprint: [r6/routes.py](../../../r6/routes.py) is already
2878 lines. SDC operations get their own blueprint mounted under the FHIR base —
consistent with how `fasten`/`shc` register their own blueprints — rather than
growing `routes.py` further.

`Questionnaire` and `QuestionnaireResponse` are added to `R6_RESOURCE_TYPES` in
[r6/validator.py](../../../r6/validator.py) so they store, validate, and read
through the existing CRUD facade (no bespoke storage).

## Endpoints

SDC-conformant; both accept and return a FHIR `Parameters` resource.

- `POST /r6/fhir/Questionnaire/$populate` (type-level)
- `POST /r6/fhir/Questionnaire/<id>/$populate` (instance-level)
- `POST /r6/fhir/QuestionnaireResponse/$extract` (type-level)
- `POST /r6/fhir/QuestionnaireResponse/<id>/$extract` (instance-level)

## `$populate` engine

**Input `Parameters`:** `questionnaire` (inline) | `questionnaireRef` | `identifier`
(resolved from the R6Resource store); `subject` (Patient reference); `content`
(extra resources for the population context).

**Process:**

1. Resolve the Questionnaire (inline → ref → identifier lookup).
2. Build the population context: `launchContext` (patient / user / encounter),
   `variable` extensions, the `content` bundle plus the fetched subject Patient.
3. Walk `Questionnaire.item` recursively. Per item, in priority order:
   - `initialExpression` extension (FHIRPath) → evaluate against context → set answer; **else**
   - `item.code` (LOINC) + observation population → query the subject's Observations
     matching the code → set answer from `value[x]`.
4. Emit a `QuestionnaireResponse` (status `in-progress`) linked to the questionnaire
   and subject. Unresolved/failed expressions are collected into an
   `OperationOutcome` returned alongside the response.

**Guardrails:** tenant-authenticated read (per the read-auth gate); AuditEvent
emitted. Optional `redaction` profile parameter runs
`apply_patient_controlled_redaction` over the returned QR answers.

## `$extract` engine

**Input:** a `QuestionnaireResponse` (inline or by id). The referencing
`QuestionnaireResponse.questionnaire` is resolved to read its extraction directives.

**Methods:**

- **Observation-based** — items (or the root) carrying `observationExtract` with an
  `item.code` produce one Observation per answered item (`value[x]`, `subject`,
  `effective`, `code` from `item.code`).
- **Definition-based** — root `definitionExtract` names the target resource type /
  profile; items carry `definition` (`StructureDefinition#element.path`) and
  `definitionExtractValue`. The engine assembles the target resource(s), mapping
  item groups to nested/array elements.

**Output:** a transaction `Bundle` (POST/PUT entries).

**Guardrails (reuse existing write path):** `$extract` requires `X-Step-Up-Token`,
runs each extracted resource through `$validate`, commits the Bundle as a
transaction (the same path as `$ingest-context`), and emits an AuditEvent. A
`dryRun=true` parameter returns the Bundle **without** committing (tenant-auth +
audit only), giving the agent a preview before the step-up commit.

## MCP tools (agent-facing)

Added to `services/agent-orchestrator/src`:

- `questionnaire_populate` — Read group. For non-public tenants, mints and forwards
  a tenant-bound token like the other reads.
- `questionnaire_extract` — Write group; step-up gated.

`Questionnaire` / `QuestionnaireResponse` reads reuse the existing `fhir_read` /
`fhir_search` (they are now stored resource types). Documented MCP tool count goes
20 → 22.

## Demo & tests

- **Seed** a sample US Core intake `Questionnaire` for `desktop-demo`: demographics
  via `initialExpression` + `definitionExtract`, and a vital via `observationExtract`.
- **Round-trip test:** seeded Patient/Observations → `$populate` → fill remaining
  answers → `$extract` → assert the resulting Bundle contents.
- **Unit tests:** expression evaluation, observation population, observation extract,
  definition extract, QR redaction.
- **Guardrail tests:** `$extract` without step-up → 401; AuditEvent emitted on both
  operations; redaction applied on populate.

## Dependencies & CI

- Add `fhirpathpy` (MIT, pure-Python FHIRPath) to `pyproject.toml`; verify it imports
  and runs under CI Python 3.11.
- New test files: `tests/test_sdc_populate.py`, `tests/test_sdc_extract.py`.

## Compliance notes

- Before merging: re-check `.claude/compliance/hipaa.md` (PHI flows through populate
  output and extract input).
- `$extract` is a write — step-up + `$validate` + AuditEvent are mandatory, matching
  the project's existing house rules.
- Telegram notifications (if wired) stay summary-level: counts/status only, never QR
  answers or extracted PHI.

## Future phases (not v1)

1. StructureMap-based populate and extract (`questionnaire-targetStructureMap`) —
   likely via an external mapping service.
2. CQL expression support in populate.
3. Template-based extraction (`templateExtract` / `templateExtractBundle`).
4. A `skills/` form-filling skill teaching personas to drive the new MCP tools.
