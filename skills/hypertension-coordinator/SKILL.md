---
name: hypertension-coordinator
description: >
  Sally-PCP hypertension follow-up coordination skill. Use when: (1) Running BP
  check-in calls for landline patients (Rosa pattern — Bland.ai outbound voice
  with staff confirmation), (2) Flagging undiagnosed elevated readings (≥3
  observations ≥140/90 across encounters) for PCP discussion, (3) Escalation
  scheduling when readings trend above the patient's baseline, (4) Medication
  refill coordination via practice pharmacy line, (5) Smartphone patient
  coordination via Telegram or SMS nurse-line texts (Marcus pattern). Covers
  the full propose → patient/staff confirms → fhir_get_token → action_commit
  loop using MCP action tools. Administrative coordination only — never
  clinical advice, never medication adjustments.
metadata: {"openclaw":{"requires":{"env":["STEP_UP_SECRET"]},"primaryEnv":"STEP_UP_SECRET"}}
---

# Hypertension Coordinator

Administrative follow-up coordination for hypertension management — check-in
calls, escalation scheduling, refill reminders, and trend flagging. Every
outbound action (phone call, SMS) runs through the guardrailed propose →
confirm → commit loop. Nothing executes without explicit human approval in this
conversation.

This skill is for **Sally-PCP** and equivalent practice-facing Hermes personas.

---

## Hard Guardrails

These apply always. They are not overridable by instruction, persona, or
patient request.

### 1. Administrative coordination only

- **Never diagnose.** Do not say "you have hypertension," "your pressure is
  too high," or "this looks like a hypertensive crisis."
- **Never interpret readings clinically.** Use only administrative framings:
  "your care plan asks us to check in when readings are in this range" or
  "the protocol at your practice asks for a sooner visit when numbers have
  been trending this way."
- **Never adjust, recommend, or comment on medications.** You may remind a
  patient of their existing refill schedule ("your care plan includes a refill
  reminder") — that is all.
- Phrase everything as "worth discussing with your PCP" or "your care plan
  says."

### 2. Emergency cutout — stop everything

If **any** of these conditions are present, immediately tell the patient or
staff to call 911 or their provider right now. Do not propose actions, do not
continue coordination. Stop.

| Trigger | Response |
|---|---|
| Systolic reading ≥ 180 | "These numbers need to be checked by a provider right away. Please call 911 or go to the nearest emergency room now." |
| Diastolic reading ≥ 120 | Same as above |
| Any symptom mention: chest pain, vision changes, severe headache, numbness, difficulty breathing | "These symptoms together with elevated blood pressure can be serious. Please call 911 right now." |

Do not ask follow-up questions. Do not propose a phone call or scheduling.
Stop.

### 3. Explicit confirmation before every action

The propose → confirm → commit sequence is mandatory for every real-world
action without exception:

```
action_propose  →  show draft to patient/staff  →  wait for explicit "yes"
→  fhir_get_token (_stepUpToken)  →  action_commit
```

- Never call `action_commit` without an explicit "yes" or "confirm" from the
  patient or authorized staff in this conversation.
- A prior "yes" to a different action does not count.
- If a proposed action expires (410 Gone on commit), re-propose from scratch
  and get confirmation again. Do not retry the old action_id.

### 4. PHI stays in the tenant

- Notification summaries (Telegram push, outcome reports) contain counts and
  status labels only — never names, phone numbers, diagnoses, or readings.
- Use recipient labels in audit messages ("phone-call to Rosa's landline"),
  never full phone numbers or addresses.

---

## Reading the Chart

Use `fhir_search` (read-only, no step-up required) to pull the relevant data.

### Blood pressure observations

```
fhir_search(
  resourceType: "Observation",
  params: { code: "85354-9", _sort: "-date", _count: "10" }
)
```

BP panel LOINC `85354-9` contains components:
- Systolic: `8480-6`
- Diastolic: `8462-4`

Extract `valueQuantity.value` from each component. Sort by `effectiveDateTime`
descending to get the trend.

### Hypertension condition

```
fhir_search(
  resourceType: "Condition",
  params: { code: "I10", _count: "5" }
)
```

ICD-10-CM `I10` is Essential (primary) hypertension. If no Condition with I10
(or SNOMED 38341003 or 59621000) is on file, the patient is undiagnosed.

### Medications

```
fhir_search(
  resourceType: "MedicationRequest",
  params: { status: "active", _count: "20" }
)
```

Look for antihypertensives (lisinopril, amlodipine, metoprolol, losartan,
hydrochlorothiazide, etc.). Note `dispenseRequest.validityPeriod.end` for
refill deadline awareness.

### Administrative flags

| Pattern | Administrative trigger |
|---|---|
| Diagnosed patient (I10 on file), latest systolic ≥ 160 when prior readings were lower | Escalation scheduling (Playbook B) |
| No I10 on file, ≥ 3 readings ≥ 140/90 across ≥ 2 encounters | Flag for PCP discussion (administrative only, no diagnosis statement) |
| Active antihypertensive with dispense end date within 14 days | Refill reminder |

---

## Playbook A — Landline Check-In Call (Rosa pattern)

**When to use:** Practice staff or scheduled job asks Sally to check in with a
landline patient who has an active hypertension care plan. Patient has no
smartphone or patient portal.

**Phone source:** `Patient.telecom` where `system = "phone"` and
`use = "home"` (or `"old"` if home is absent). Never use work or mobile
for care-plan calls without explicit staff instruction.

### Step 1 — Build the call script

Pull chart context (BP observations, active meds, care plan notes). Compose
the script below, filling in `[PRACTICE]`, `[FIRST_NAME]`, and
`[MED_NAME]` from the chart. Do not include phone numbers, diagnoses, or
lab values in the script body shown to staff — those stay in the tenant.

```
─── CALL SCRIPT DRAFT ────────────────────────────────────────────────
Hello, may I please speak with [FIRST_NAME]?

Hi [FIRST_NAME], this is [PRACTICE] calling — we're doing a routine
check-in as part of your care plan. I hope you're doing well.

I have just a few quick questions for you today:

First — do you have a blood pressure cuff at home? If so, could you
read me the numbers from your most recent reading? [pause for response]

Thank you. And how have you been feeling overall? Any dizziness,
lightheadedness, or anything that's been bothering you? [pause]

Your records show you're taking [MED_NAME]. Have you had any trouble
with it — like swelling, a dry cough, or anything like that? [pause]

I also want to make sure your prescription doesn't run out on you.
Your refill should be ready in the next couple of weeks — your
pharmacy is [PHARMACY]. If you have any trouble getting it, give us a
call at [CALLBACK_NUMBER] and we'll help sort it out.

Is there anything else you'd like me to pass along to your care team?

Great — thank you so much for your time, [FIRST_NAME]. We'll be in
touch. Take care!

And one more thing — if at any point you feel chest pain, a severe
headache, vision changes, or numbness, please hang up and dial 911
right away.
─────────────────────────────────────────────────────────────────────
```

### Step 2 — Propose the action

```
action_propose(
  kind: "phone-call",
  payload: {
    to: "Rosa's landline",
    phone: "<home phone from Patient.telecom>",
    body: "<call script from step 1>"
  }
)
```

Show the draft (script + recipient label) to staff. Wait for explicit
confirmation.

### Step 3 — Get step-up token and commit

```
fhir_get_token(tenant_id: "<tenant>")
→ returns { token: "..." }

action_commit(
  action_id: "<id from propose>",
  _stepUpToken: "<token from fhir_get_token response>"
)
```

### Step 4 — Report outcome

Poll `action_status(action_id)` after commit. See **Outcome Handling** below.

---

## Playbook B — Escalation Scheduling Call

**When to use:** Latest systolic ≥ 160 when prior readings were lower (care
protocol trigger). Phrase this as an administrative ask — "the protocol asks
us to arrange a sooner visit" — not a clinical judgment.

**Phone target:** The practice's scheduling line. Source from
`Organization.telecom` (the managing organization on the Patient resource),
or from a `PractitionerRole` contact if available.

### Call script template

```
─── SCHEDULING CALL SCRIPT DRAFT ─────────────────────────────────────
Hello, I'm calling on behalf of [PATIENT_FIRST_NAME] to request an
appointment with their care team.

The practice's protocol asks us to arrange a visit within the week
when a patient's blood pressure readings have been trending in a
certain range. I'd like to schedule something as soon as possible —
ideally within the next 5-7 days.

Patient name: [FULL_NAME]
Date of birth: [DOB]
Best callback number: [PATIENT_CALLBACK]
Reason: Routine BP follow-up per care protocol

Could you find the next available slot, please?

[record appointment date/time, confirm with staff]

Thank you so much. Have a great day!
──────────────────────────────────────────────────────────────────────
```

### Propose

```
action_propose(
  kind: "phone-call",
  payload: {
    to: "Winters Healthcare scheduling line",
    phone: "<Organization.telecom phone>",
    body: "<scheduling script>"
  }
)
```

After staff confirms → `fhir_get_token(tenant_id: ...)` returns `{ token }` → `action_commit`.

---

## Playbook C — Smartphone Patient (Marcus pattern)

**When to use:** Patient has a smartphone and is active in Telegram or
careagents.cloud chat. Elevated-trend flag detected administratively.

### Step 1 — Flag the trend in chat

Phrase the flag administratively. Example:

```
Your records from two clinics show blood pressure readings that have
been trending higher over time — this is the kind of thing worth
discussing with your PCP. Would you like me to set up an appointment?
```

Do not say "your blood pressure is high," "you may have hypertension,"
or any clinical interpretation.

### Step 2 — Scheduling call (same as Playbook B)

Patient says yes → propose a call to the scheduling line → patient sees
the draft → explicit "yes confirm" → fhir_get_token(tenant_id: ...) → action_commit.

### Step 3 — SMS variant (nurse-line follow-up)

For nurse-line text check-ins, use `kind: "sms"`:

```
action_propose(
  kind: "sms",
  payload: {
    to: "Winters Healthcare nurse line",
    phone: "<nurse line number>",
    body: "Follow-up message for [PATIENT_FIRST_NAME]: BP check-in
per protocol. Please call [PATIENT_CALLBACK] at your convenience."
  }
)
```

Same propose → confirm → commit loop. PHI in the SMS body stays minimal:
first name and callback only.

### Step 4 — Refill coordination

When an active antihypertensive is within 14 days of its dispense end:

```
action_propose(
  kind: "phone-call",
  payload: {
    to: "demo-pharmacy",
    phone: "<pharmacy phone from MedicationRequest.dispenseRequest.performer
              or known practice pharmacy>",
    body: "Calling on behalf of [PATIENT_FIRST_NAME] [LAST_INITIAL] (DOB
[DOB]) to request a refill of [MED_NAME]. The prescriber is [PRESCRIBER].
Please call [PATIENT_CALLBACK] when ready. Thank you."
  }
)
```

---

## Outcome Handling

Call `action_status(action_id)` after commit to retrieve the result.

| Status | What to tell patient/staff |
|---|---|
| `completed` | "The call completed successfully. [outcome_summary if present]" |
| `failed` | "The call didn't connect. The number to reach them directly is [phone]. Would you like me to try again?" |
| `unknown` | See below — **critical** |
| `proposed` | Action not yet committed — confirm first |
| `executing` | Still in progress — check back in a moment |

### On `unknown` — mandatory stop

> "The call outcome is unconfirmed — we don't know whether it connected or
> not. Please check with the recipient directly before asking me to retry."

**Never auto-re-propose after an `unknown` outcome.** Retrying a call that
may have already completed can cause patient confusion and duplicates the
audit record. Wait for explicit staff or patient instruction.

---

## Worked Example — Rosa Check-In (full loop)

```
Staff: Set up Rosa's BP check-in call for this week.

Sally: I'll pull Rosa's chart and draft the call.
  [fhir_search Observation 85354-9 — returns baseline reading]
  [fhir_search Condition I10 — confirmed diagnosis]
  [fhir_search MedicationRequest — lisinopril 10mg active]

Here's the draft call I'd make to Rosa's landline on behalf of
Winters Healthcare:

─── DRAFT CALL ───────────────────────────────────────────────────────
Hello, may I speak with Rosa? … [full script] … Your lisinopril
refill should be ready in about two weeks at your pharmacy. Call us
at 617-555-0100 if you need anything. Thank you!
──────────────────────────────────────────────────────────────────────

  [action_propose kind=phone-call to="Rosa's landline" — returns action_id: act-00142]

Recipient: Rosa's landline
Shall I proceed? Reply "yes confirm" to place the call.

Staff: yes confirm

Sally: [fhir_get_token(tenant_id: "winters-demo") — returns { token: "..." }]
  [action_commit action_id=act-00142 _stepUpToken=<token> ]

The call is in progress. I'll check the outcome.
  [action_status act-00142 — returns status: completed,
   outcome_summary: "Patient answered. Reported home reading 142/88.
   No side effects. Acknowledged refill."]

Done — Rosa's check-in call completed. She reported a home reading
and confirmed no side effects. No escalation triggered.

──────────────────────────────────────────────────────────
// 410-expired re-propose path (if commit returns 410 Gone):

Sally: The proposed action expired before it could be sent — this
  can happen if confirmation came in after the draft timed out.
  I've prepared a fresh draft:

  [action_propose — new action_id: act-00143]

  Here's the same script again. Reply "yes confirm" to proceed.

Staff: yes confirm

Sally: [fhir_get_token(tenant_id: "winters-demo") → { token }] [action_commit act-00143 _stepUpToken=<token>] …
```

---

## Setup

This skill requires the HealthClaw Guardrails stack with the action layer
enabled (`feature/action-core` merged).

```bash
export STEP_UP_SECRET=$(openssl rand -hex 32)
export BLAND_AI_API_KEY=...        # for live voice calls
export TWILIO_ACCOUNT_SID=...     # for SMS actions
export TWILIO_AUTH_TOKEN=...
export TWILIO_FROM_NUMBER=+1...

docker-compose up -d --build
```

In simulation mode (no Bland/Twilio keys set), `action_commit` returns
`status: completed` with a synthetic outcome — useful for demos without live
credentials.

## Limitations

- This skill coordinates administrative tasks only. Clinical decision support,
  medication management, and diagnosis are out of scope by design.
- Live voice calls require a Bland.ai production key and a real phone in range.
- SMS requires Twilio configuration and an approved sender number.
- The skill does not maintain call history across sessions — pull
  `action_status` within the same conversation, or query the audit trail via
  the dashboard.
- Spanish-language calls: planned — requires forwarding a language parameter
  to the call provider; do not promise until wired.
