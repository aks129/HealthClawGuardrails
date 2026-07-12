# Forms Rail (W2) Implementation Plan — Aug-18 demo centerpiece

> Subagent-driven TDD. Builds on the merged action rail + SDC. Scope: `docs/superpowers/plans/2026-07-12-forms-rail-scope.md`.

**Goal:** a `form-fill` ActionExecutor: SDC `$populate` fills a standard intake Questionnaire from the patient's FHIR data → **structured per-item review** (meds/allergies confirmed individually; "no known allergies" NEVER inferred) → completed PDF → shareable delivery (DocumentReference + link). No external providers. Demoable: "your AI fills your intake from your records, you approve each item, it's a shareable PDF."

## Architecture decisions (LOCKED)

1. **Questionnaire:** author a HealthClaw canonical intake (`healthclaw-intake`, replacing the 3-field demo seed) with: demographics (Patient), current medications (repeating, MedicationRequest), allergies (repeating, AllergyIntolerance + an explicit `no-known-allergies` boolean), problems/conditions (repeating, Condition). SDC-shaped (item.code/definition for population; launchContext=patient).
2. **PDF engine:** `reportlab` (in-runtime; generalize `r6/smbp/report.py:render_pdf`). NOT headless-Chrome.
3. **Review surface:** a new authenticated, server-rendered page at `GET /r6/actions/<id>/review` + `POST /r6/actions/<id>/review`. Per-item confirm for every med/allergy/condition; the `no-known-allergies` box must be affirmatively checked and is never auto-set; absent allergy data renders "not reviewed with patient," never blank/NKA. On submit → the reviewed QuestionnaireResponse is stored + an `ActionConfirmation` is issued → the action's confirm/execute path runs. This IS ceremony tier-1 (clinical content → dashboard review flow, never a chat button).
4. **Delivery (v1):** `execute()` renders the reviewed QR → PDF, persists a `DocumentReference` carrying the base64 PDF, and returns a shareable link (reuse the SHL/`$share-bundle` path if it can carry the DocumentReference; otherwise a signed download route). Full server-side SHL encryption is a stretch, not required for the demo.
5. **NKA policy:** allergies section defaults to "not reviewed"; NKA requires the explicit human affirmation; populate NEVER fills NKA from absent data.
6. **`payload.body`** for a form-fill proposal = human-readable summary ("Complete new-patient intake for <clinic> from your records"). `payload` also carries `questionnaire` (canonical/id) + optional `clinic` label.

## Tasks (subagent-driven, TDD)

- **T1 — Intake Questionnaire** (`r6/sdc/intake.py` or seed): author the canonical `healthclaw-intake` with demographics/meds/allergies/conditions items + the NKA boolean; store/serve by id; unit test asserts structure + that no NKA default exists. Foundational; blocks T2, T3, T6.
- **T2 — Populate for list resources**: extend `r6/sdc/populate.py` so AllergyIntolerance/MedicationRequest/Condition populate their repeating items; add an explicit "not asserted" state; **NKA never inferred** (load-bearing test). Depends T1.
- **T3 — `form_fill` ActionExecutor** (`r6/actions/rails/form_fill.py`): `kind='form-fill'`, `validate` (requires questionnaire ref + body), `execute` (orchestrates render→DocumentReference→link), `reconcile`; register in `register_all`; passes the generic contract suite. Depends T1.
- **T4 — QR→PDF renderer** (`r6/sdc/pdf.py`): generalize `smbp/report.py` to render an arbitrary completed QuestionnaireResponse to a reportlab PDF with a provenance footer ("populated from records by an automated system, reviewed by patient on <date>"). Big/independent — parallelizable.
- **T5 — DocumentReference w/ embedded PDF bytes** (extend `smbp/routes.py:_persist_document_reference`): base64 `attachment.data`, tenant-scoped R6Resource. Depends T4.
- **T6 — Structured review page** (`GET/POST /r6/actions/<id>/review` + template): per-item confirm, explicit NKA affirmation, Approve disabled until every med/allergy acted on; on submit issues ActionConfirmation + stores reviewed QR. Big/core-safety. Depends T1,T2.
- **T7 — Delivery link** (SHL hand-off or signed download route for the PDF DocumentReference). Depends T5.
- **T8 — execute() orchestration**: propose→review→confirm→populate→(reviewed QR)→render→DocumentReference→link, mapped to ExecutionResult; `EXTRACTION_AMBIGUOUS`/`STALE_SOURCE_DATA` where they apply. Depends T2–T7.
- **T9 — Tests + demo path**: rail contract auto-coverage, populate-with-lists, **NKA-never-inferred** (load-bearing), PDF/DocumentReference, review-flow, end-to-end propose→review→execute on synthetic data.

Ship each on a branch → PR → review → merge. Feature freeze Aug 11.
