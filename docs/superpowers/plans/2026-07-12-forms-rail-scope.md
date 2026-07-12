# Forms Rail (W2) — Scope (parked pending distribution push)

Scoped 2026-07-12 against the merged action rail + SDC code. Build deferred while the ecosystem/distribution work takes priority. This is the grounded starting point when it resumes.

**Goal:** a `form-fill` ActionExecutor: SDC `$populate` fills a standard intake questionnaire from the patient's FHIR data → **structured per-item review** (meds/allergies confirmed individually; "no known allergies" NEVER inferred) → completed PDF → SMART Health Link delivery. Builds on the out-of-band confirm flow. No external providers needed.

## Plumbing already present (reusable)
- `form-fill` is already a `VALID_KIND` (proposable, fails loud at confirm since unregistered).
- `$populate` engine: `r6/sdc/populate.py:33 populate_questionnaire()` — but matches **Observation only** (`_observation_answer`), not AllergyIntolerance/MedicationRequest.
- `$extract` documents "the form-fill review IS the HITL step" (`r6/sdc/routes.py:118-123`) — the rail must actually BE that review.
- `reportlab` is a runtime dep; `r6/smbp/report.py:99 render_pdf()` is the PDF pattern to generalize. (No headless-Chrome in runtime deps.)
- Error taxonomy already reserves `EXTRACTION_AMBIGUOUS`, `STALE_SOURCE_DATA`.
- DocumentReference persist pattern: `r6/smbp/routes.py:137` (stores size only — embedding base64 PDF bytes is net-new).
- SHL is **client-side only today** (`shl_generate` MCP tool → `$share-bundle` + client encryption). Server-side SHL, or a defined hand-off carrying a PDF DocumentReference, is net-new.

## Genuinely-absent pieces
1. A real intake Questionnaire (current `healthclaw-intake` seed is 3 demographic fields; needs meds/allergies/conditions with per-item confirmable structure and no NKA default).
2. Populate for list resources (AllergyIntolerance/MedicationRequest → repeating items) with an explicit "not asserted" state.
3. The `form_fill` ActionExecutor (`r6/actions/rails/form_fill.py`) + `register_all` wiring.
4. Structured per-item review surface (net-new; confirm today exposes only a PHI-safe summary).
5. PDF-embedding DocumentReference (base64 `attachment.data`).
6. Server-side SHL or a defined client hand-off.

## Task order (deps)
T1 intake Questionnaire → T2 populate-lists (+NKA-never-inferred) → T3 executor skeleton+register → T4 QR→PDF (big) ∥ → T5 DocumentReference+bytes → T6 structured review (big, core safety) → T7 SHL delivery → T8 execute() orchestration → T9 tests (NKA-safety test load-bearing).

## Decisions to settle before building
1. Which intake Questionnaire (author HealthClaw canonical vs adopt US-Core/SDC intake profile).
2. PDF engine: reportlab (in-runtime, reuse smbp/report.py) vs headless-Chrome (heavy new dep). Lean reportlab.
3. Where the per-item review lives (new authenticated endpoint + server-rendered page vs Telegram inline vs command-center). Biggest product decision + core safety control.
4. SHL architecture (keep client-side hand-off vs build server-side encryption/manifest). Does SHL carry the rendered PDF DocumentReference + the structured QR?
5. `payload.body` semantics for form-fill (propose requires non-empty body).
6. "No known allergies" encoding so NKA requires explicit human affirmation, never auto-populated. Correctness invariant — pin before T2.
