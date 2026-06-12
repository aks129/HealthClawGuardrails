# Hypertension Demo Plan — Winters Healthcare (Gigi / Yimdriuska Magan)

**Status:** design draft, 2026-06-12
**Owner:** Eugene; clinical scenarios per Gigi's 2026-06-12 email
**Target:** polished demo ready within ~2-3 weeks (Winters Healthcare response expected next week)

## Why this maps perfectly onto the stack

Gigi's two scenarios are exactly the two front-ends HealthClaw already has:

| Scenario | Surface | What it proves |
|---|---|---|
| **1. Landline-only patient** | Bland.ai outbound voice calls via the new action layer (`r6/actions/`) | Equity story: chronic-condition management for patients with zero apps, zero portals, zero smartphones — the system calls *them* |
| **2. Smartphone patient** | Telegram (Sally-PCP persona) + careagents.cloud web chat | Full platform: connected records, AI coordination, one-tap approvals |

Hypertension is the right clinical vehicle: prevalent, underdiagnosed, and the management protocol is administrative — scheduled BP checks, medication adherence follow-ups, escalation to the practice when readings trend high. Everything the agent does is coordination, not clinical advice, which keeps us inside the guardrail story.

## Scenario 1 — Landline patient ("Rosa, 72")

**Setup:** Rosa was discharged with a new lisinopril prescription and a diagnosis of stage-1 hypertension. She has a landline. Her chart (synthetic, seeded into a `winters-demo` tenant) has the Condition, MedicationRequest, and a baseline BP Observation.

**Flow (5 min):**

1. **Protocol kicks in** — practice staff (or a scheduled job, Phase 2) asks Sally: *"Set up Rosa's BP check-in call for this week."*
2. **Propose** — Sally drafts the call script from chart context: greeting, identifies as calling on behalf of Winters Healthcare, asks Rosa to read her home cuff numbers, asks about dizziness/med side effects, reminds her of the refill. Staff sees the script in Telegram. PHI stays in the tenant-scoped draft; the audit trail records only "phone-call to Rosa's landline, proposed."
3. **Confirm** — staff replies "yes confirm" → step-up token + `X-Human-Confirmed` → Bland.ai dials the landline. *(Demo: real call to a phone in the room — this is the showpiece. Fallback: simulation mode narrated.)*
4. **Webhook resolves** — call transcript summary lands in `outcome_summary`; Telegram push says "✅ phone-call to Rosa: completed" (no PHI).
5. **Escalation branch** — Rosa reported 162/98. Sally proposes the follow-up action per protocol: a call to the practice's scheduling line to book her within the week. Same propose → confirm → call loop.

**The line that sells it:** "Rosa never installed anything. Her care plan runs on the phone she's had for forty years — and every call the AI made is in an append-only audit log."

## Scenario 2 — Smartphone patient ("Marcus, 48")

**Setup:** Marcus has a smartphone, undiagnosed HTN, and elevated readings in his record from two systems (seeded via the Fasten/HBO connect story).

**Flow (6 min):**

1. **Connect** — Marcus taps the connect link in Telegram; records stream in through the guardrail layer (existing `/connect` + redaction + audit demo from the Dev Days runbook).
2. **Detection** — Sally reviews context: three elevated BP observations across two providers, no hypertension Condition on file. She flags it: *"Your readings have been trending high — this is worth discussing with your PCP. Want me to set up the appointment?"* (Administrative framing; no diagnosis.)
3. **Action menu** — Marcus says yes. Sally proposes: call to PCP's scheduling line. Approve in one tap. *(careagents.cloud shows the same flow in a web UI for the in-room screen.)*
4. **Refill + follow-through** — two weeks later (narrated), Marcus is on lisinopril; the refill-by-phone and "text the nurse line" flows show the recurring loop. The SMS path runs through Twilio with the same propose/confirm gates.
5. **The QR close** *(if Phase 4 lands by demo day, else narrate)* — Marcus generates a SMART Health Link QR carrying his BP history + insurance for the new cardiologist's front desk.

## What exists today vs. what to build

**Exists (on `feature/action-core`, PR #19):**
- Full action lifecycle (propose/commit/execute/webhook) with Bland + Twilio executors, simulation mode, audit, step-up + 428 — Python 597 tests green
- MCP tools (`action_propose` / `action_commit` / `action_status`) usable from Hermes/OpenClaw personas
- Telegram personas, connect flows, redaction/audit demo material (Dev Days runbook Acts 1–3)

**To build for this demo (in order):**
1. **Seed script** — `winters-demo` tenant: Rosa + Marcus charts (Conditions, MedicationRequests, BP Observations with a trend) — extend `scripts/seed_demo_tenant.py` patterns (~half day)
2. **Sally hypertension skill** — `skills/` SKILL.md encoding the HTN follow-up protocol: when to offer a check-in call, escalation thresholds phrased administratively, refill cadence (~1 day, mostly prompt work + the action-tool wiring that now exists)
3. **Bland production key + `ACTIONS_WEBHOOK_SECRET` on Railway** — and one rehearsed live call to a real handset (~half day incl. test calls)
4. **Demo polish** — careagents.cloud agent page themed for the smartphone scenario; runbook with fallbacks like the Dev Days one (~1 day)
5. *(Stretch)* scheduled outbound check-ins (cron → propose, staff confirms each morning) and the SHL QR close

Total: ~3-4 working days of build, comfortably inside Gigi's window.

## Open questions for Gigi

1. Live call on stage vs. pre-recorded? (We can do live — simulation fallback is built in.)
2. Should the landline scenario show *staff* confirming actions (practice-led) or a family-member confirmer? Changes who holds the Telegram chat.
3. Does Winters want to see the escalation threshold logic, or keep clinical protocol off-screen and show coordination only? (Recommend the latter — keeps us clearly on the administrative side.)
4. Spanish-language call script for Rosa? Bland supports it; good equity beat if their population fits.
