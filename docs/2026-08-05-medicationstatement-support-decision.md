# MedicationStatement at ingest — what shipped, and what needs a ruling

Issue #377. Written by Dev, for the CTO and Product. The issue asks for two
things and they are separable. One shipped. One needs a decision this role
does not hold.

## What shipped

An ingest that discards a resource now names the type it discarded.

`skipped` was one integer covering every type the store does not keep. All
three ingest paths reported it that way, so "which type did we drop?" had no
answer in the audit trail or in the log. #377's own precondition — check the
counters before building anything — could not be met with the counters we
had.

| Path | Where the answer appears now |
| --- | --- |
| `stream_ingest` (Fasten EHI export) | `fasten_import_complete` audit detail, plus the completion log line |
| `_ingest_bundle` (SHC bridge) | `shc_import_complete` audit detail, the log line, and the returned counts |
| `$ingest-context` | `skipped_count` and `skipped_types` in the 201 response, plus the audit detail |

`POST /r6/fhir/internal/ingest-bundle` already named the type per entry
(#227). It is unchanged.

The type name is built from `_NAMEABLE_SKIPPED_TYPES`, a code-owned set in
`r6/fasten/ingester.py`. `resourceType` is caller-supplied on every line of a
real export, and audit `detail` is exported into the auditor-facing
compliance bundle, so the name is constructed rather than repeated. An
unlisted type counts under `other`. It still counts.

## What did not ship, and why

`MedicationStatement` is still not in `R6Resource.SUPPORTED_TYPES`.

Adding it to the set is one line. Making it reach a patient is not, and
shipping the one line alone makes the product worse in a specific way: the
resource would be stored, no read surface would read it, and the skip
counter that just started naming it would stop. A visible hole becomes an
invisible one. That is the defect shape in `docs/2026-08-02-retro.md`, moved
one layer down.

### The half that is clean

- The store keeps canonical JSON. It has no per-type schema.
- `apply_redaction` walks fields, not types. It needs no profile.
- `label_codings` is keyed by code. An RxNorm coding on a
  `MedicationStatement` gets the same label it gets anywhere else.
- The CapabilityStatement and the direct-upload allowlist both derive from
  `SUPPORTED_TYPES`.
- `r6/validator.py` needs a `_validate_medication_statement` for parity with
  `MedicationRequest`. That is mechanical: status, medication, subject.

### The half that is a modelling decision

FHIR R6 separates the two on purpose. A `MedicationRequest` is an order. A
`MedicationStatement` is a record of what the person actually takes. A
patient asking "what am I on?" is asking about statements. A patient asking
"what did my doctor prescribe?" is asking about requests. Both questions
reach the same tool today.

Three open questions, in order of how much they cost to get wrong.

**1. Deduplication cannot be done by code alone.** A source that sends a
request and a statement for the same drug has sent one medication. Two
statements carrying the same RxNorm code may be a dose change over time, so
collapsing them loses a real fact. Worst: a statement whose only
identification is free text has no code to match on, and we cannot read that
text — redaction strips it, correctly. Dedup by code therefore works on
exactly the records we can already read and fails silently on the ones we
cannot. That is the wrong failure direction for a medication list.

**2. The intake form is the highest-stakes surface.** `r6/sdc/` populates the
medication section from active `MedicationRequest` resources. A patient
reviews and attests to that list. Changing where it draws from changes what
a person signs. It needs a clinician's opinion, not a developer's.

**3. The copy is Product's.** Two named groups in a chat answer means new
patient-facing wording, thirteen days before the webinar, on an issue ranked
P2 (`docs/2026-08-05-prioritized-backlog.md` row 19).

## Recommendation

**Store both. Keep them apart. Never merge them.**

The double-counting question only exists if the two are flattened into one
list, and flattening is the thing FHIR says not to do. Presenting them as two
named groups answers "what am I taking?" honestly and needs no dedup rule.

Sequenced:

1. **Measure first.** Read the new `skipped_types` in
   `fasten_import_complete` for live tenants. If no connected source sends
   `MedicationStatement`, this stays a readiness gap and waits. The issue
   asked for this check and it is now possible.
2. **If a live feed sends it:** add the type, add the validator case, add it
   to the `search_records` enum, and read it in `get_health_summary` under
   its own key. Product owns the two labels the patient sees.
3. **Leave the intake form on `MedicationRequest`** until a clinician rules
   otherwise. The review card is the guardrail on that path and its source
   should not change without one.

Not recommended: a merged medication list, a dedup rule keyed on RxNorm, or
any path that lets a statement stand in for a request.
