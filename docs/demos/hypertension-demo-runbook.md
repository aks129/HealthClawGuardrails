# Hypertension Demo — Live Runbook

**Audience:** Gigi / Yimdriuska Magan · Winters Healthcare
**Format:** 15–20 min live demo, two scenarios
**One-line pitch:**
> "Chronic-condition coordination for every patient — including the ones with only a landline."

**Goal:** Show the full action lifecycle (propose → confirm → call) against real hypertension patient data, narrating every guardrail along the way. Rosa proves the equity story: no app, no portal, no smartphone — the system calls her. Marcus proves the platform story: connected records, cross-clinic trend detection, one-tap approve.

---

## Pre-flight (do 30 min before you go on)

### 1. Confirm Railway services are healthy

```bash
curl -s https://app.healthclaw.io/r6/fhir/health | jq .status
curl -s https://mcp-server-production-5112.up.railway.app/health | jq .
```

Both must return `"ok"` / `{"status":"ok"}`. If not: `railway logs --service HealthClawGuardrails`.

### 2. Seed the winters-demo tenant

Run against Railway (HTTP mode, preferred — matches what the audience sees):

```bash
python scripts/seed_demo_tenant.py \
  --tenant-id winters-demo \
  --bundle-file scripts/demo_bundles/winters_hypertension.json \
  --base-url https://app.healthclaw.io
```

Or run locally if Railway is unavailable:

```bash
python scripts/seed_demo_tenant.py \
  --tenant-id winters-demo \
  --bundle-file scripts/demo_bundles/winters_hypertension.json \
  --db-mode
```

Verify seed landed:

```bash
curl -s -H "X-Tenant-Id: winters-demo" \
  "https://app.healthclaw.io/r6/fhir/Patient?_summary=count" | jq .total
# expect 2 (Rosa + Marcus)
curl -s -H "X-Tenant-Id: winters-demo" \
  "https://app.healthclaw.io/r6/fhir/Observation?_summary=count" | jq .total
# expect 6 (3 Rosa BP readings + 3 Marcus BP readings)
```

### 3. Check env vars

```bash
# Required for action commits (step-up HMAC — every commit 401s without this):
echo $STEP_UP_SECRET           # must be non-empty on Railway

# Required for webhook callbacks (callbacks 403 without this):
echo $ACTIONS_WEBHOOK_SECRET   # must be non-empty on Railway

# Live voice calls (set ONLY if doing the live Bland.ai call):
echo $BLAND_AI_API_KEY         # leave unset → simulation mode, still fully demoable

# SMS path (Marcus scenario):
echo $TWILIO_ACCOUNT_SID
echo $TWILIO_AUTH_TOKEN
echo $TWILIO_FROM_NUMBER
```

**Bland key absent:** `action_commit` returns `status: completed` with a synthetic outcome. Narrate: "In simulation mode, the voice engine is stubbed — in production, Bland.ai places the real call. Let me show you what the response looks like either way."

### 4. Swap in the room handset for Rosa's 555 number

Rosa's seeded phone is `617-555-0147` — a synthetic number that will never ring. Before the demo:

1. Note the real handset number you've brought (a real mobile in the room).
2. When Sally drafts the call script in Act 1, **edit the proposed payload** to use the real handset number instead of `617-555-0147`.
3. The commit will dial the real handset. Answer on speaker — that's the money shot.
4. Never let the 617-555-0147 number go to Bland live. If you forget, the call will fail gracefully; narrate the `failed` → retry path as a feature.

### 5. Confirm Telegram and tabs

- Send `/health` to the Telegram bot bound to `winters-demo` tenant. Expect `Flask: OK / MCP: OK`.
- Telegram push must be bound: `TELEGRAM_BOT_TOKEN` set on Railway, bot bound to `winters-demo` tenant.

Pre-open tabs:

| Tab | URL |
|---|---|
| Telegram (Sally-PCP) | web.telegram.org or phone |
| Dashboard / audit trail | `https://app.healthclaw.io/r6-dashboard` |
| careagents.cloud | agent page for winters-demo (Marcus scenario) |
| Railway logs | `railway logs --service HealthClawGuardrails` in a terminal |

---

## Act 1 — Rosa, the Landline Patient (7 min)

**Setup framing (say before touching keyboard):**

> "Rosa is 72. She was discharged two weeks ago with a new hypertension diagnosis and a lisinopril prescription. She doesn't have a smartphone. She doesn't have a patient portal. She has the landline she's had for forty years. Winters Healthcare's staff member — let's call her the care coordinator — opens Telegram and asks Sally, the AI coordinator, to set up Rosa's BP check-in."

---

### Step 1 — Staff asks for the check-in call

In Telegram, type:

```
Set up Rosa's BP check-in call for this week.
```

- **Point out:** Sally pulls the chart silently — you'll see `fhir_search` calls against `winters-demo` for Observation, Condition, MedicationRequest.
- **Say:** "Sally reads Rosa's three BP readings (158/96 baseline, 150/92 at one week, 162/98 yesterday), confirms the I10 hypertension Condition is on file, confirms lisinopril 10mg is active, and drafts the call script."

---

### Step 2 — The proposed call script appears

Sally returns the draft (from Playbook A in the hypertension-coordinator skill):

```
─── DRAFT CALL ────────────────────────────────────────────
Hello, may I please speak with Rosa?

Hi Rosa, this is Winters Healthcare Family Practice calling —
we're doing a routine check-in as part of your care plan. ...

... Your lisinopril refill should be ready in about two weeks
at Neighborhood Pharmacy. Call us at 617-555-0100 if you
need anything.
─────────────────────────────────────────────────────────────
Recipient: Rosa's landline
Shall I proceed? Reply "yes confirm" to place the call.
```

- **Point out:** The script shown to staff does NOT contain Rosa's phone number, her blood pressure values, or her date of birth. Those stay tenant-scoped.
- **Say:** "The audit trail right now has one entry: `ProposedAction — phone-call to Rosa's landline`. Not her number. Not her readings. The PHI is in the tenant store; the audit event is safe to export."
- **Point out (show dashboard in side tab):** Filter audit trail to `winters-demo`, event type `ProposedAction` — one entry just appeared.

---

### Step 3 — Staff confirms → step-up → LIVE CALL

Type in Telegram:

```
yes confirm
```

- **Say:** "Sally now needs a step-up token — a short-lived HMAC-signed credential that authorizes this specific write. It calls `fhir_get_token`, then `action_commit`. The MCP bridge sets `X-Human-Confirmed: true` on every commit request — the human gate is the skill's mandatory rule that action_commit is only CALLED after explicit confirmation in this conversation. Any direct caller that omits the header gets HTTP 428 Precondition Required."
- Watch the call connect. **Let the handset ring. Answer on speaker.**
- The AI voice runs the check-in script live.
- **Say:** "Rosa never installed anything. The system called her."

---

### Step 4 — Webhook resolves, Telegram push arrives

After the call ends, Bland.ai fires a webhook to Railway. Within a few seconds, Telegram shows:

```
✅ phone-call to Rosa's landline: completed
```

- **Point out:** No phone numbers. No medication names. No readings. Just status and a recipient label.
- **Say:** "That's PHI rule four in the skill: notification summaries are counts and status labels only. The full outcome — what Rosa reported, any notes — lives in the tenant store behind the same guardrail stack."
- **Simulation mode caveat:** If `BLAND_AI_API_KEY` is not set, there is NO outbound call and therefore NO webhook callback and NO Telegram push. The commit resolves synchronously with a synthetic outcome, but the push beat does not happen. This is expected — narrate: "In simulation mode, the commit returns a synthetic completed status directly; in production the webhook fires from Bland.ai and the Telegram push follows."

---

### Step 5 — Escalation beat (Rosa's 162/98)

- **Say:** "That third reading — 162/98 — is above the baseline trend. The protocol at Winters Healthcare asks for a sooner visit when readings tick up like that. Watch what Sally proposes next."

In Telegram (Sally may surface this automatically, or type):

```
Rosa's latest reading was elevated. What does the protocol say?
```

Sally proposes a scheduling call to the Winters Healthcare scheduling line (617-555-0100) per Playbook B:

```
─── SCHEDULING CALL DRAFT ─────────────────────────────────
Hello, I'm calling on behalf of Rosa to request an appointment
with her care team.

The practice protocol asks us to arrange a visit within the
week when a patient's readings have been trending in this
range. Next available slot, please — ideally within 5-7 days.

Patient: Rosa Delgado · DOB: 1954 · Callback: Rosa's landline
Reason: Routine BP follow-up per care protocol
─────────────────────────────────────────────────────────────
Recipient: Winters Healthcare scheduling line (617-555-0100)
```

- **Say:** "Notice the framing — 'the protocol asks for a sooner visit.' Not 'her blood pressure is dangerously high.' Not 'she may be having a hypertensive crisis.' Administrative coordination only. The clinical interpretation stays with Dr. Winters."
- Confirm or leave as simulated (your call — escalation call to 617-555-0100 goes nowhere live, so narrate it).

---

## Act 2 — Marcus, the Smartphone Patient (6 min)

**Setup framing:**

> "Marcus is 47. He has a smartphone and he's connected through Telegram. He has a diabetes diagnosis but no hypertension diagnosis — yet. Here's the thing: he has three elevated BP readings in the record, from two different organizations."

---

### Step 1 — Check conditions

In Telegram, type:

```
/conditions
```

- **Point out:** The Condition list shows Type 2 diabetes (E11.9). No I10 — no hypertension diagnosis on file.
- **Say:** "The data is right there. Three readings above 140/90 across Winters Clinic and Bayview Urgent Care. But no one has connected the dots because the systems don't talk to each other. This is the detection story."

---

### Step 2 — Sally reviews and flags the cross-clinic trend

In Telegram, type:

```
Review Marcus's BP history.
```

Sally runs `fhir_search` for Observations, returns the trend (142/88 from winters-clinic, 146/91 from bayview-urgent-care, 149/94 from winters-clinic — all ≥ 140/90, across two organizations), and flags it:

```
Your records from two clinics show blood pressure readings that
have been trending higher over the past three months — this is
the kind of thing worth discussing with your PCP. Would you
like me to set up an appointment?
```

- **Point out:** "Two organizations" — not "you have hypertension." Not a clinical statement. Purely administrative.
- **Say:** "Sally never says 'you have hypertension' — that's hard-wired in the skill. The emergency cutout is in there too: if Marcus had mentioned chest pain or a reading above 180/120, Sally stops everything and tells him to call 911. Nothing goes to Bland."

---

### Step 3 — One-tap approve → scheduling call

Marcus (you) type:

```
Yes, please set up the appointment.
```

Sally proposes a scheduling call to 617-555-0100 (Winters Healthcare). Show the draft — same Playbook B template but with Marcus's details.

- Confirm with "yes confirm" → step-up → `action_commit`.
- **Say:** "Same propose → confirm → commit loop, same step-up gate, same 428 guard. The fact that Marcus is on a smartphone instead of a landline doesn't change the guardrail architecture."
- Switch to the careagents.cloud tab.
- **Say:** "careagents.cloud is the web UI surface for the same flow — for clinics that want an in-browser experience rather than Telegram. The agent, the skill, the guardrails are identical."

---

### Step 4 — SMS beat (nurse-line follow-up)

In Telegram, type:

```
Text the nurse line to flag Marcus for a follow-up.
```

Sally proposes an SMS action (kind: `sms`) to the Winters Healthcare nurse line:

```
─── SMS DRAFT ────────────────────────────────────────────
To: Winters Healthcare nurse line
Body: Follow-up for Marcus: BP check-in per protocol.
Please call Marcus's mobile at your convenience.
─────────────────────────────────────────────────────────
```

- **Say:** "Same loop. SMS goes through Twilio with the same propose/confirm/commit gates. PHI in the body stays minimal — first name and callback label only."
- If Twilio is configured: confirm and show the outbound SMS. If not: narrate simulation mode.

---

## Act 3 — The Guardrail Close (3 min)

Switch to the dashboard tab (`https://app.healthclaw.io/r6-dashboard`).

Filter audit trail to tenant `winters-demo`, event type `ProposedAction` — all action audit events use `resource_type='ProposedAction'`; create (propose) and update (claim, resolve) events all appear under this type.

**Point out each entry:**

1. `ProposedAction — create — phone-call — winters-demo` (Rosa check-in proposed)
2. `ProposedAction — update — phone-call — winters-demo — status: completed` (Rosa check-in resolved)
3. `ProposedAction — create — phone-call — winters-demo` (Rosa escalation proposed)
4. `ProposedAction — create — phone-call — winters-demo` (Marcus scheduling proposed)
5. `ProposedAction — update — phone-call — winters-demo — status: completed` (Marcus scheduling resolved)

**Say:** "This is an append-only audit log. There is no UPDATE, no DELETE on AuditEvent rows — enforced at the SQLAlchemy layer. Every action that touched a patient has a record: who proposed it, what step-up token authorized it, what the outcome was."

**On the step-up token:**

> "The commit endpoint returns HTTP 428 Precondition Required if `X-Human-Confirmed: true` is absent. The MCP bridge sets that header on every commit call — the human gate is the skill's mandatory rule that action_commit is only CALLED after explicit confirmation in this conversation. Any direct API caller that omits the header gets 428. Two layers: the skill (prompt-level) and the server (protocol-level)."

**On atomic claim:**

> "When action_commit runs, it atomically claims the action_id. A second commit on the same ID returns 409 Conflict. You cannot double-dial Rosa by hitting confirm twice."

**On the `unknown` status philosophy:**

> "If the Bland webhook is delayed — network hiccup, Bland rate limit — the action comes back `unknown`. We never flip that to `failed` automatically. We never auto-retry. Why? Because an auto-retry means Rosa's phone rings twice. She answers the second call mid-conversation with her family and hears a robot. That's not a guardrail failure — that's a patient trust failure. We tell staff: check back manually. The call may have completed."

---

## Fallbacks

| Situation | Recovery |
|---|---|
| Bland key not set | Simulation mode — `action_commit` returns `status: completed` with synthetic outcome synchronously. **No webhook fires, no Telegram push.** Narrate: "In simulation mode there is no outbound call and no webhook callback — the commit resolves immediately with a synthetic outcome. In production, Bland.ai places the real call, the webhook fires on completion, and the Telegram push follows." |
| Live call fails on stage (wrong number, no answer) | `status: failed` or `unknown` appears in Telegram. **Narrate this as a feature:** "This is exactly the outcome the guardrail stack handles — failed shows the number to dial manually, unknown means don't retry. The system never auto-retries a phone call." |
| Railway down | Run locally: `python main.py` + seed with `--db-mode`. Update `--base-url` to `http://localhost:5000`. Demo is identical. |
| Webhook slow / action stays `executing` | Poll manually: `action_status(action_id)` in Telegram, or show the dashboard ProposedAction entry and narrate the polling pattern. Webhook latency is also a feature story: the system waits for ground truth, doesn't assume success. |
| Handset doesn't ring | Switch to pre-recorded clip (prepare a 60-second Bland.ai call recording before the demo). Play it while narrating the script. Show the webhook resolve and Telegram push using the recording's synthetic outcome. |
| Marcus conditions query returns nothing | Check tenant header — `winters-demo`, not `desktop-demo`. Re-seed if needed. Fallback: show the raw `fhir_search` call in a terminal and walk through the JSON. |
| careagents.cloud not loading | Skip the web-UI beat; stay in Telegram. The guardrail story is fully told without it. |

---

## Open Items

- **Bland production key + per-minute cost:** Confirm key is live on Railway before demo day. Bland.ai outbound calls bill per minute — budget ~5 minutes of call time for rehearsals + 2-3 minutes on stage. Note the cost in the Winters Healthcare follow-up email so there are no surprises in production.
- **Rehearse the live call twice:** Dial the real handset at least twice before going on stage. Check that the Bland voice completes the full script (it sometimes truncates on first attempt if the call connects before the script is fully loaded). Verify the webhook fires and Telegram push appears within 10 seconds.
- **Staff-confirmer vs. family-confirmer for Rosa:** The demo has a care coordinator (staff) confirming Rosa's actions. Open question for Gigi: does Winters Healthcare want to show a family member holding the Telegram chat instead — a son or daughter who is listed as a care partner? Changes who the "yes confirm" comes from; the action layer supports both patterns. Flag this in the follow-up call.
- **Spanish script variant:** Spanish-language calls are planned but not yet wired — forwarding a language parameter to the call provider is not yet implemented; do not promise this in the demo until confirmed available. Good question to raise with Gigi but flag it as roadmap, not current.
- **Escalation call target:** The scheduling call in Act 1 Step 5 dials `617-555-0100` (winters-clinic). That's a synthetic number — confirm with Gigi whether they want to see a real practice scheduling line dialed, or whether the simulation narration is sufficient for that beat.

---

## After the Demo

If Gigi's team wants to explore the codebase or run it themselves:

```
git clone https://github.com/aks129/HealthClawGuardrails
cd HealthClawGuardrails && uv sync && python main.py
# seed winters-demo: python scripts/seed_demo_tenant.py --tenant-id winters-demo \
#   --bundle-file scripts/demo_bundles/winters_hypertension.json --db-mode
# → http://localhost:5000
```

Key references to share:

- Action lifecycle: `r6/actions/` (propose/commit/execute/webhook)
- Hypertension coordinator skill: `skills/hypertension-coordinator/SKILL.md`
- Seed data: `scripts/demo_bundles/winters_hypertension.json`
- Dashboard: `https://app.healthclaw.io/r6-dashboard`
- careagents.cloud: agent web-UI surface

The closing line, if the room is quiet:

> "Rosa never installed anything. Her care plan runs on the phone she's had for forty years — and every call the AI made is in an append-only audit log."
