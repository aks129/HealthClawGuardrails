# Dev Days — Live Demo Runbook

**Presentation:** "OpenClaw for Healthcare: Guardrails, Trust, and Patient Empowerment"
**Format:** 10–15 min live demo + Q&A
**One-line pitch (from Bo Holland, CEO Health Bank One):**
> "With one connection, users would never need to manually fill out forms or remember passwords again."

**Goal:** show the full end-to-end flow in a single Telegram session — patient consent →
records streaming in → Curatr quality check → approved fix → /summary → **form auto-fill** —
while narrating every guardrail that fires along the way.

**Demo theme:** HealthClaw is the guardrail OS between Health Bank One (data + identity) and
AI agents. HBO owns the data pipeline; HealthClaw enforces who can see it, when, and under
what authorization.

---

## Pre-flight (do 30 min before you go on stage)

### 1. Confirm Railway services are healthy

```bash
curl -s https://app.healthclaw.io/r6/fhir/health | jq .status
curl -s https://mcp-server-production-5112.up.railway.app/health | jq .
```

Both must return `"ok"` / `{"status":"ok"}`. If not, `railway logs --service HealthClawGuardrails`.

### 2. Confirm bot is online

Send `/health` to your Telegram bot. Expect:

```
Flask: OK (mode=local)
MCP: OK
```

### 3. Check record count — have real data ready

```bash
curl -s -H "X-Tenant-Id: my-tenant" \
  "https://app.healthclaw.io/r6/fhir/Condition?_summary=count" | jq .total
```

If zero → either the data hasn't been loaded or the tenant name differs.
Fall back to `desktop-demo` (pre-seeded synthetic data) — just change
`TENANT_ID=desktop-demo` in your bot env and redeploy.

### 4. Verify Fasten webhook is pointed at Railway

In [portal.connect.fastenhealth.com/developers](https://portal.connect.fastenhealth.com/developers):
- Webhook URL: `https://app.healthclaw.io/fasten/webhook` ✓
- Click **Send test event** → confirm "Fasten test webhook received" in Railway logs

### 5. Open tabs in advance (avoids on-stage typing)

| Tab | URL |
|---|---|
| Telegram | web.telegram.org or phone |
| Connect page | `https://app.healthclaw.io/connect/my-tenant` |
| Dashboard | `https://app.healthclaw.io/r6-dashboard` |
| PromptOpinion agent | `https://app.promptopinion.ai` (optional, for MCP live demo) |
| Railway logs | `railway logs --service HealthClawGuardrails` in a terminal |

---

## Demo Script

### Act 1 — Patient Onboarding (2 min)

**Narrate:** "The patient just downloaded our Telegram bot. They type `/start`."

1. In Telegram, send `/start`
   - **Point out:** bot replies with a confirmation that "Chat is bound to tenant `my-tenant`"
   - **Say:** "Under the hood, OpenClaw just made a step-up-authenticated POST to
     `/internal/bind-telegram`. The server verified a time-limited HMAC token before
     accepting the binding — that's the step-up authorization pattern."

2. Send `/connect`
   - Bot replies with `https://app.healthclaw.io/connect/my-tenant`
   - **Say:** "This is the Fasten TEFCA page. One click and the patient verifies their
     identity through CLEAR or ID.me across every QHIN in the network."
   - Open the connect page in your pre-loaded browser tab, show the Stitch widget
   - **Don't actually run the authorization live unless you have a real live connection ready**;
     instead say "I already authorized before coming on stage — let's watch what happened."

---

### Act 2 — Records Arriving + Guardrails (3 min)

3. Send `/conditions`
   - Bot returns the redacted condition list
   - **Point to:** patient name shows as initials (e.g. `E. V.`), not full name
   - **Say:** "Every read path runs through HIPAA Safe Harbor redaction. Names become
     initials, dates of birth truncate to year, identifiers are masked. The raw PHI never
     leaves the FHIR store unredacted."

4. Switch to the dashboard tab (`/r6-dashboard`)
   - Show the real-time resource counts grid
   - **Say:** "Every one of those reads also wrote an AuditEvent. The audit trail is
     append-only — no UPDATE, no DELETE on AuditEvent rows, enforced at the SQLAlchemy
     layer."

5. In a terminal, tail the Railway logs:
   ```bash
   railway logs --service HealthClawGuardrails 2>&1 | grep "AuditEvent\|redact\|tenant"
   ```
   - **Show:** `audit_event` lines with `X-Tenant-ID: my-tenant`
   - **Say:** "Tenant isolation is enforced on every database query — the `my-tenant`
     tag is baked into the WHERE clause, not trusted from the client."

---

### Act 3 — Curatr Data Quality (3 min)

6. Send `/curatr`
   - **Say:** "Curatr is our agentic data-quality layer. It evaluates the loaded
     Conditions, MedicationRequests, and Immunizations against known clinical patterns —
     contradictions, missing required fields, implausible titer values."
   - Show a finding (e.g. "tobacco status contradicts immunization titers")

7. Send `/curatr fix`
   - Bot shows the proposed fix and asks for `/approve`
   - **Say:** "The MCP `fhir_propose_write` tool flagged this as a clinical type requiring
     human-in-the-loop confirmation. The server returned HTTP 428 Precondition Required
     until we supply `X-Human-Confirmed: true`. That header is what `/approve` adds."

8. Send `/approve`
   - **Show:** "Fix applied. Status: `updated`"
   - **Say:** "And a Provenance resource was written linking the fix to the Curatr agent.
     Every mutation in this system has a cryptographic audit trail."

---

### Act 4 — MCP in Claude / PromptOpinion (2 min, optional)

9. Switch to PromptOpinion or Claude Desktop
   - **Say:** "HealthClaw also speaks the MCP protocol directly. Any compliant AI host —
     PromptOpinion, Hermes, Claude Desktop — can call our 19 tools with the same
     guardrails active."
   - Call `fhir_search` for Conditions live, show the redacted result with `_mcp_summary`
   - **Say:** "The SHARP-on-MCP standard lets the AI host pass `X-FHIR-Server-URL` and
     a SMART token — HealthClaw then forwards to whatever upstream FHIR server the patient
     authorized, with our guardrail stack in the middle."

---

### Act 5 — Health Bank One + Form Auto-Fill (3 min) ⭐ STRONGEST DEMO

**Setup:** you've already authorized via QR code on your phone. HBO has pulled your full
record (conditions, meds, insurance, identity) from every provider/payer.

1. Show the HBO connection in Claude Code or Desktop:
   - **Say:** "Health Bank One's MCP server is running at `mcp.app.healthbankone.com/mcp`.
     I authorized once by scanning a QR code with their banking-grade digital ID app.
     Now every AI tool I authorize — Claude, ChatGPT, Hermes — can query my records."

2. Pull the tool catalog live:

   ```bash
   python scripts/export_healthbankone_mcp.py \
     --tenant-id my-tenant --discover --pretty
   ```

   - **Show:** tools listed at runtime (health.summary, medications, conditions, identity, …)
   - **Say:** "No hardcoded API calls. The script discovers whatever HBO exposes at runtime —
     same pattern we use with HealthEx."

3. **Form auto-fill demo (the money shot):**
   - Show a 40-page patient intake form URL (any publicly accessible form — Epic MyChart
     registration, insurance prior auth, or a simple Google Form for demo purposes)
   - **Say:** "Healthcare's biggest patient burden is paperwork. The average intake form
     is 40 pages. Bo Holland — HBO's CEO — put it best on our call yesterday:
     *'With one connection, users would never need to manually fill out a form or
     remember a password again.'*"
   - Narrate (or if live connection: demonstrate) HealthClaw querying HBO via MCP,
     mapping fields to form inputs, and submitting on the patient's behalf
   - **Say:** "HealthClaw takes the form URL, queries HBO for the relevant data,
     auto-populates every field, and submits with an audit trail. The patient approves
     in Telegram with one tap."

4. Show Curatr correction loop (if time):
   - **Say:** "When Curatr finds a data error — say a tobacco status that contradicts
     immunization titers — it generates a correction letter. In the HBO model, that letter
     goes back through HBO's fax pipeline to the provider who wrote the bad record.
     Healthcare has never had a dispute resolution mechanism like this. Financial services
     has had it for 50 years."

---

### Wrap-up talking points (30 sec)

- **Open source** — fork it, deploy on Railway in 5 minutes
- **Vendor-neutral** — HealthEx, Fasten, Health Bank One, any SMART/FHIR server
- **Three data sources, one guardrail layer** — HealthEx (pull), Fasten (push), HBO (identity-verified pull)
- **Pattern library, not a product** — copy the guardrail patterns into your own stack
- **PromptOpinion marketplace** — live today at `app.promptopinion.ai/marketplace`
- **GitHub** — `github.com/aks129/HealthClawGuardrails`
- **Contact:** `developer@healthbankone.com` if you want into the Bootstrap Program

---

## Fallbacks

| Problem | Recovery |
|---|---|
| Bot not responding | Check `railway logs --service openclaw-bot`; restart service |
| `/connect` page shows "key not configured" | `FASTEN_PUBLIC_KEY` not set; pivot to showing the r6-dashboard + audit trail instead |
| Curatr finds nothing | Use `desktop-demo` tenant which has pre-seeded synthetic data with a tobacco contradiction |
| Railway down | Run locally: `python main.py` in one terminal, demo against `localhost:5000` |
| HBO endpoints not live yet | Skip Act 5; use the discovery script output from a previous dry run as a screenshot |

---

## After the Demo

If audience wants to try it:

```
Quickstart in 3 commands:
  git clone https://github.com/aks129/HealthClawGuardrails
  cd HealthClawGuardrails && uv sync && python main.py
  # → http://localhost:5000
```

Or point them at the hosted demo at `app.healthclaw.io` and the PromptOpinion marketplace.
