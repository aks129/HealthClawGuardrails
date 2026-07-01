# Winters SMBP Demo — Platform Capabilities Design

**Date:** 2026-06-18
**Status:** Approved (design); pending implementation plan
**Author:** Eugene Vestel + Claude Code
**Source:** Beluma Health "Hypertension Demo Specification — Two Patient Scenarios"
(internal working doc, June 2026, target audience: Winters Healthcare)

## Goal

Build the net-new HealthClaw platform capabilities so the two Winters self-measured
blood-pressure (SMBP) demos run on **real product** — a smartphone-messaging patient
(Marisol) and a landline-voice patient (Mr. Ray) — both following the same clinical arc
(borderline office BP → 14 days home monitoring → clinician report → treatment/follow-up)
and both ending on a **real clinician-facing SMBP report**. No mockups: the demo films real surfaces.

This assembles on top of capabilities already in the repo:
- `skills/hypertension-coordinator` — Sally-PCP triage bands, emergency cutout, propose→confirm→commit loop, landline + smartphone patterns.
- `r6/actions/` — `phone-call` (Bland.ai) + `sms` (Twilio) action layer with the human-confirm guarantee.
- FHIR Observations, the step-up + audit guardrail stack, `reportlab` (already a dependency), and the command-center (per-tenant ops dashboard with access tokens).

## Scope

**Phase 1 (this build) — core, gets a fileable end-to-end demo:**
1. Centralized clinical logic (triage bands + symptom screen + emergency cutout).
2. SMBP monitoring sessions + readings as FHIR Observations + adherence math.
3. The clinician SMBP report (HTML endpoint + reportlab PDF, as a FHIR DocumentReference).
4. Bilingual (EN/ES, ≤6th-grade) patient content catalog.
5. Two channels wired onto the existing action layer: Demo 1 (Telegram/SMS) and Demo 2 (Bland.ai voice with read-back + keypad).

**Phase 2 (fast-follow, out of scope here):**
- Photo-of-cuff **OCR** via the Anthropic vision API (confidence threshold + confirm-back), feeding the same Observation log.
- The **escalation → booking → telehealth-conversion** beat: structured scheduling request → **Option A** staff one-tap confirm surface in the command-center → agent relays the slot → telehealth phone-visit conversion → close-the-loop care-team notification.

**Decisions locked during brainstorming (2026-06-18):**
- Capture recognition is **real**, not simulated: cuff-photo OCR via Anthropic vision (phase 2); Demo 2 spoken read-back + keypad via the existing Bland.ai voice agent (no new ASR stack).
- Report is **HTML endpoint + PDF export**, surfaced as a FHIR `DocumentReference`.
- Booking is **Option A** (structured request + staff one-tap confirm) — phase 2.
- The report is **computed directly from Observations**, not via SDC `$extract` (it isn't a form).

## Architecture

New module `r6/smbp/`, mirroring `r6/actions/` / `r6/sdc/` / `r6/fasten/`. Pure transform
logic stays Flask-free and unit-testable; the route layer owns auth, audit, step-up, and store I/O.

```text
r6/smbp/
  __init__.py
  triage.py      [pure] §2.2 triage bands + 7-symptom screen + emergency cutout — single source of truth
  monitoring.py  [pure] 14-day SMBP session model: AM/PM schedule, valid-day + adherence math
  report.py      compute AM/PM + overall average vs 130/80 + flags; render HTML + reportlab PDF
  content.py     [pure] bilingual (EN/ES, ≤6th-grade) message catalog
  routes.py      Flask blueprint: enroll, log-reading, report endpoint, DocumentReference; auth/audit/store I/O
```

Registered on the app in `main.py` alongside the other blueprints.

## Component detail

### 1. `triage.py` — centralized clinical logic (the credibility play)

A pure module encoding §2.2 **exactly**. Clinicians will check this; it must be the one
implementation the skill, the voice script, and the report flags all call, so they cannot drift.

Aligned to the **2025 AHA/ACC** guideline (Gigi Magan spec, updated June 2026). Two
**independent axes** — the BP number and red-flag symptoms — evaluated separately.

- `classify(systolic, diastolic, symptoms=None) -> TriageResult` returning band, agent-action, rationale.
- BP-axis bands (no symptoms): `at_goal <130/80` (log/encourage), `stage1 130–139/80–89`
  (log + flag, no patient alarm), `stage2 140–179/90–119` (symptom screen → if negative, arrange
  visit within the week), `crisis ≥180/120` (re-check in 5 min → if still high, same-day care-team contact).
- **Symptom axis (independent):** ANY red-flag symptom → emergency / 911, **regardless of the reading**.
  Red-flag screen (7): chest pain, shortness of breath, one-sided weakness/numbness, trouble speaking,
  vision change, severe headache, confusion. These are stroke/heart-attack signs; the agent must never
  reassure a symptomatic patient because the number looks normal.
- Home diagnostic threshold is **130/80** (the 2025 line; 135/85 was the older threshold tied to the
  retired 140/90 office cutoff) everywhere a threshold is referenced.

### 2. `monitoring.py` — SMBP session + adherence

- A lightweight SMBP session: patient/tenant, 14-day window, AM + PM slots/day; ≥12 valid days required for a valid average.
- Readings are FHIR **Observations**: BP panel LOINC `85354-9` with components systolic `8480-6`
  and diastolic `8462-4` (UCUM `mm[Hg]`), tagged with timestamp and AM/PM. Stored via the existing
  Observation write path (step-up + audit).
- `adherence(session, observations) -> {completed, prescribed, rate}` (e.g. 24 of 28).
- `averages(observations) -> {am, pm, overall, valid_days}` — pure computation over the window.

### 3. `report.py` — clinician SMBP report (the "wow" / last frame)

- Computes the per-reading table (timestamp, AM/PM, systolic/diastolic, band flag), AM/PM averages,
  overall average **displayed against 130/80**, adherence %, and a flag summary.
- Renders a **one-page HTML** report (served at a route for clean screen capture / browser viewing)
  and a **reportlab PDF** export (the artifact the clinic keeps).
- The generated report is stored as a FHIR `DocumentReference` for the tenant.
- Read-shaped: tenant-read-authenticated + AuditEvent.

### 4. `content.py` — bilingual patient content

- EN/ES message catalog at ≤6th-grade reading level: enrollment/consent prompts, picture-teaching step
  captions, reminders, reading-confirmation read-backs, medication education (lisinopril: what it's for,
  when to take, plain-language side effects = dizziness on standing / dry cough → "tell your care team,
  don't stop on your own"), the symptom screen, and Q&A framing ("ask your care team" on anything clinical).
- Patient language preference is set at enrollment and carried on the session.
- Administrative-only: content never diagnoses or adjusts treatment.

### 5. `routes.py` — Flask blueprint

- `POST /r6/smbp/enroll` — start a session for a patient (tenant header; language preference; consent capture flag). Tenant-authenticated.
- `POST /r6/smbp/reading` — log a reading → classify via `triage` → store Observation → return triage result + localized confirmation. Step-up + audit (it's a write).
- `GET  /r6/smbp/report/<session_id>` — render the clinician report (HTML; `?format=pdf` for PDF). Tenant-read-authenticated + audit; persists a DocumentReference.
- Outbound patient contact (reminder SMS, Bland voice call) is **not** issued directly here — it goes through the existing `r6/actions/` propose → human-confirm → commit loop, so the demo's every-write-requires-confirmation guarantee holds.

### Channel wiring (reusing the action layer + skill)

- **Demo 1 (smartphone / Marisol):** Telegram/SMS reminders + capture (typed reading / voice note now;
  photo OCR is phase 2). Adaptive reminder timing (patient tells the agent their shift changed). Uses the
  existing `sms` action kind + Telegram push. Bilingual via `content.py`.
- **Demo 2 (landline / Mr. Ray):** Bland.ai outbound `phone-call` that guides the reading by voice
  (mirroring the printed sheet), captures the spoken BP with **read-back confirmation + keypad fallback**,
  and runs the symptom screen by voice. The Bland pathway/script is the net-new config; spoken capture is
  handled by Bland natively (real, not simulated).

## Guardrails (reused invariants — not new primitives)

- **Administrative-only:** the agent never diagnoses, never interprets readings clinically, never adjusts
  or recommends medications. Framing is "your care plan says…" / "worth discussing with your PCP."
- **Symptom axis (independent):** ANY red-flag symptom → 911, **regardless of the BP reading** —
  a symptom is a stroke/heart-attack sign; the agent never reassures a symptomatic patient because
  the number looks normal. Encoded in `triage.py`, non-overridable.
- **Every outbound action human-confirmed** via the existing action layer loop.
- **All PHI synthetic/composite** — no detail traceable to a real patient.

### Clinical interpretation decision (RESOLVED 2026-06-27)

Earlier the source spec was internally inconsistent on the emergency cutout (the §2.2 table read
symptom-escalates-only-at-≥180/120, the prose read "OR any symptom"). Gigi's **updated, 2025 AHA/ACC-
aligned spec resolves it authoritatively**: the BP number and red-flag symptoms are **two independent
axes**, and **any red-flag symptom is an emergency regardless of the reading**. That is what
`r6/smbp/triage.py` now implements (symptoms evaluated first, independently). The home threshold also
moved to **130/80** and Stage 2 to **140/90** per the same 2025 update, and the red-flag screen is **7**
symptoms (adds *confusion*).

## Seed + tests

- A synthetic `winters-demo` tenant: composite **Marisol** (smartphone, Spanish, office 145/92→139/89)
  and **Mr. Ray** (landline, English, known elevated BP), each with ~14 days of synthetic readings —
  Marisol trending to a 138/88 overall average (hypertension confirmed → lisinopril 10mg), Mr. Ray with a
  164/98 escalation reading for the Demo 2 centerpiece.
- **Unit tests (pure):** `triage.classify` across all five bands + every symptom; `monitoring.adherence`
  and `averages` (incl. <12-valid-days handling); `report` computation (averages vs 130/80, flag counts).
- **Route tests:** enroll, log-reading (step-up required; Observation written; triage returned),
  report endpoint (HTML + PDF; DocumentReference persisted; tenant-read auth; audit emitted).
- **Guardrail tests:** emergency-cutout reading classified to the emergency pathway; reading write without
  step-up → 401; AuditEvent emitted on report read and reading write.

## Out of scope (this spec)

- Phase 2 items above (OCR, booking → telehealth, staff confirm surface).
- The demo **videos** themselves (a separate production effort once the real flows exist).
- The open engineering questions in §6 of the source spec that are pure product/compliance decisions
  (WhatsApp BAA stance, TCPA consent UX, report delivery into Epic/OCHIN) — noted for follow-up, not built here.

## Future phases

1. **Phase 2** — photo-of-cuff OCR (Anthropic vision, confidence + confirm-back); escalation → Option-A
   staff-confirm booking in the command-center → telehealth phone-visit conversion → care-team close-the-loop.
2. Epic/OCHIN report delivery (PDF to in-basket / media tab) for a real pilot.
3. WhatsApp channel (pending the Meta BAA compliance decision); SMS-with-consent is the conservative default.
