# Forms rail — demo run-of-show

**The story in one line:** *An AI agent fills your intake form from your real
health records → you review each medication and allergy individually (the
software can never claim "no known allergies" for you) → you get a shareable,
provenance-stamped PDF.*

This is the Aug-18 webinar centerpiece. It runs entirely on **synthetic data**
and needs no phone/SMS provider — every step is real, server-side, and
guardrailed. Total runtime ~6 minutes.

## Why it lands

The audience has seen "AI fills a form" demos. What they have *not* seen is a
form-filling agent that **structurally cannot fabricate a clinical assertion**.
The load-bearing beat is the allergy review: removing every allergy without
explicitly attesting "no known allergies" is rejected *server-side* (HTTP 422),
and a crafted request can't dodge it because the server re-derives the item list
from FHIR rather than trusting the client. Silence about allergies is never
consent. That is the whole pitch — safety you can watch fail closed.

## One-command readiness check

Before the webinar (and any time you touch the deployment), run the smoke test —
it drives this whole path end to end and fails loudly on any broken beat:

```bash
python scripts/demo_smoke.py                 # live prod (app.healthclaw.io)
python scripts/demo_smoke.py --base http://127.0.0.1:5000   # local stack
```

Green all the way down means the demo is ready to present.

## Pre-flight (before you present)

1. Stack up locally or point at a demo deployment:
   ```bash
   STEP_UP_SECRET=dev-secret PUBLIC_BASE_URL=http://localhost:5000 python main.py
   ```
   `PUBLIC_BASE_URL` **must** be set — the rail fails loud (`provider_not_configured`)
   without it, by design. That's a feature you can show, but for the happy-path
   demo, set it.
2. Seed synthetic records for the demo tenant: `POST /r6/fhir/internal/seed`.
   Confirm the patient has at least one medication and one real allergy
   (Penicillin) so the review page has something to confirm.
3. Mint a step-up token: `POST /r6/fhir/internal/step-up-token {"tenant_id": "desktop-demo"}`.
4. Open the guardrail scorecard in a browser tab you can flip to:
   `GET /r6/fhir/$conformance?format=text` — the local FHIR profile should show
   Grade A, with all seven properties passing.

## Run of show

| Beat | You do | You say |
|------|--------|---------|
| **1. The ask** | In the agent (Claude Desktop / any MCP client), ask it to *"fill out my new-patient intake form from my records."* The agent calls `action_propose` (kind `form-fill`). | "I'm not uploading anything. The agent is reading my *own* FHIR records through the guardrail layer." |
| **2. Propose ≠ do** | Show the action is `awaiting_confirmation`, not done. | "It proposed. It did **not** submit. Nothing an agent's own tools can do will approve its own action — that's the out-of-band gate." |
| **3. The review page** | Open `GET /r6/actions/<id>/review`. Each medication and each allergy is its own row, populated *from the record* with provenance. The **"No known allergies (patient confirmed)"** box is **unchecked**. | "It filled the form, but every clinical line waits for me. Notice the allergy box is empty — the software did not decide that for me." |
| **4. The safety beat** *(the money shot)* | Try to submit having **removed** the Penicillin allergy but **without** checking the NKA box. It's rejected — **422**, no approval issued, form not finalized. | "Watch. I remove my allergy and try to submit without attesting. The server refuses. It re-checked my records — I can't quietly drop a real allergy, and it will never *assume* I have none." |
| **5. Honest submit** | Confirm the Penicillin allergy (or check NKA if that were true), confirm meds, submit. The review page issues the confirmation. | "Now I attest, item by item. This is the human-in-the-loop, and it's recorded." |
| **6. Execute** | The out-of-band confirm (`POST /r6/actions/<id>/confirm`) runs `execute()`: reviewed answers → PDF → FHIR `DocumentReference` → signed link. Show the action now `completed` with a `delivery_link`. | "Only *after* I approved does it render anything. No approval, no PDF — it fails closed." |
| **7. The artifact** | Open the `delivery_link` in a fresh browser (no login, no headers — the signature in the URL is the credential). The PDF opens with the provenance footer: *populated from records by an automated system, reviewed by the patient on <date>.* | "Here's the shareable form. It's stamped with exactly how it was made and that a human reviewed it. That footer is the difference between a document a clinic can trust and one it can't." |
| **8. Prove it** | Flip to the `$conformance` tab: Grade A (7/7), including Error Fidelity. | "None of this is 'trust me.' The guardrails grade themselves A–F in CI, including strict rejection, lenient warnings, and their audit evidence." |

## Fallbacks if something misbehaves

- **Link 404s:** the DocumentReference is tenant-scoped — make sure the browser
  link's `t=` matches the demo tenant. Re-run from beat 6.
- **422 on the honest submit too:** you left a medication row un-actioned. Every
  med and allergy row must have a decision; that's intentional.
- **`provider_not_configured`:** `PUBLIC_BASE_URL` isn't set on the server. Set
  it and restart. (Or lean in: "this is the rail refusing to run half-configured.")
- **No allergy to confirm:** the seed didn't include one. Re-seed, or fall back
  to the NKA path — but the allergy-confirm path is the stronger story.

## The two live polls (audience engagement)

1. *"Would you trust an AI agent to fill a medical form for you?"* — ask **before**
   beat 4. (Most say no.)
2. *"Now that you've seen it fail closed on the allergy — would you?"* — ask
   **after** beat 8. The shift is the talk.

## One-line takeaway to close on

"Agents are going to touch medical records whether the industry is ready or not.
The question isn't *if* — it's whether the guardrails are open, inspectable, and
provable. This is. Come build the next rail with us."
