# HL7 SDC implementer feedback — ready to submit

**Where:** https://jira.hl7.org → **Create**
**Project:** FHIR Specification Feedback
**Issue Type:** Change Request
**Specification / Work Group:** `FHIR-sdc` (Structured Data Capture)
**Reported by:** eugene.vestel@gmail.com (your HL7 account)

**Required — "at least one of Related Artifact(s) or Related Page(s)":**
- **Related Page(s):** `https://hl7.org/fhir/uv/sdc/extraction.html` (SDC Data Extraction page)
- **Related Artifact(s)** (optional, stronger): autocomplete → `QuestionnaireResponse/$extract`,
  or URL `https://hl7.org/fhir/uv/sdc/OperationDefinition-QuestionnaireResponse-extract.html`

---

## Summary (paste into the Summary field)

```
Definition-based extraction: clarify element-path write semantics (nested elements, arrays, choice types) with worked examples
```

## Description (paste into the Description field)

```
We implemented both $populate (expression-based + observation-based) and $extract (observation-based + definition-based) in an open-source server (HealthClaw Guardrails, https://github.com/aks129/HealthClawGuardrails) and found definition-based extraction the hardest mechanism to implement interoperably, because the write semantics of item.definition element paths are underspecified.

The IG says the definition "specifies the element the answer maps to" (e.g. http://hl7.org/fhir/StructureDefinition/Patient#Patient.name.given), but as an implementer it is unclear:

1. Array handling — when the path traverses a repeating element (Patient.name.given), should the extractor create name[0].given[0], append to an existing repetition, or is grouping determined by the enclosing group item? Multiple answers to the same item vs. repeated group items seem to imply different structures, but this is not stated.

2. Intermediate structure creation — when a path like Patient.contact.name.family is written into an empty resource, presumably every intermediate element is created; whether extractors are expected to support arbitrary depth (and how to signal partial support) is not specified.

3. Choice types — how a definition should address a choice element (e.g. Observation.value[x]) — by the [x] name or the concrete type name — is not shown in any example.

4. Conformance floor — there is no statement of a minimum path grammar an extractor MUST support, so two conformant implementations can disagree on whether a given Questionnaire is extractable. Our v1 shipped with an explicitly documented subset (name.*, birthDate) precisely because we could not determine the required floor.

Suggestion: add a short section (or expand the existing definition-based extraction section) with (a) a defined path grammar and required-support floor, (b) 3-4 worked examples covering a repeating element, a nested backbone element, and a choice type, and (c) expected behavior when the path cannot be written (error vs. skip + OperationOutcome warning — we chose skip-with-warning but found no guidance).

Happy to contribute draft examples from our implementation if useful.
```

---

**Why this one:** definition-based extraction is a genuine interop gap we hit in
production — substantive implementer feedback that WGs act on, and the kind that
gets contributors named in IG acknowledgments. (Submitted via the web form
because jira.hl7.org's REST create screen is disabled for this project.)
