# Quickstarts — use HealthClaw from your AI agent

HealthClaw Guardrails is a remote MCP server. Any agent that speaks MCP can
connect to it and work with health records behind enforced guardrails (PHI
redaction, audit trail, human-in-the-loop, disclaimers).

**Connector URL (live production server):**

```text
https://mcp-server-production-5112.up.railway.app/mcp
```

No API key needed to try it: without credentials you get the `desktop-demo`
tenant — a synthetic demo patient panel with realistic conditions, labs,
immunizations, and medications. **Nothing in it is real patient data, which
makes it safe to demo on camera.**

## Pick your agent

| Agent | Guide | Works on phone? |
| --- | --- | --- |
| Claude (claude.ai, desktop, iOS/Android) | [claude.md](claude.md) | Yes — add the connector on the web once, it appears in the mobile app |
| Perplexity (Pro/Max) | [perplexity.md](perplexity.md) | Connector added in settings; available wherever you use Perplexity |
| ChatGPT (Plus/Pro, Developer Mode) | [chatgpt.md](chatgpt.md) | Dev-mode connectors work in the ChatGPT apps |
| Telegram (OpenClaw bot) | [telegram.md](telegram.md) | Yes — pure chat |
| Claude Code / any MCP client | [mcp-generic.md](mcp-generic.md) | — |

WhatsApp and iMessage are not supported yet (no MCP surface there today);
Telegram is the chat-app path.

## The 10-minute demo script (works in any connected agent)

Say these to your agent, in order. Each one exercises a different part of the
stack. Everything runs against the synthetic demo tenant.

1. **Confirm the connection**
   > What HealthClaw tools do you have available? List them by group.

2. **Record summary**
   > Give me a summary of the health record — patients, conditions,
   > observations, medications. Use the HealthClaw tools.

3. **Lab interpretation** (decision support, never diagnosis)
   > Interpret the recent lab results. Anything out of range? Explain in
   > plain language.

4. **Preventive care gaps** — the "what am I due for?" question
   > What preventive care is this patient due for? Check the care gaps.

5. **Data quality (Curatr)**
   > Run a data-quality check on the observations and conditions in this
   > record. Any coding problems?

6. **Next-steps synthesis**
   > Based on everything you found — the labs, the care gaps, the data
   > quality — what are the recommended next steps? Note what needs a
   > clinician.

7. **The guardrails money shot** — show safety is enforced, not promised
   > Run the guardrail conformance check and show me the grade.

   (Returns a live A–F scorecard proving PHI redaction, audit, step-up,
   human-in-the-loop, tenant isolation, disclaimers, and error fidelity are
   active.)

8. **Show that writes are gated**
   > Try to write an observation to the record.

   The agent will hit the step-up + human-confirmation gate — that 428 is
   the feature. Nothing is written without cryptographic authorization and
   an explicit human yes.

9. **Share a record safely** (SMART Health Links)
   > Generate a secure share link for this patient's record.

## Connecting your own health data (Fasten Connect)

The demo tenant is synthetic. To put YOUR records behind the guardrails:

1. Open `https://app.healthclaw.io/connect/<your-tenant-id>` — pick a long,
   unguessable tenant id (e.g. `jane-k8f3q9w2`); it names your private,
   isolated data space.
2. Click connect. The Fasten Stitch widget walks you through **identity
   verification (CLEAR or ID.me)** and connecting your providers — TEFCA
   network access means one verification covers participating systems.
3. When the connection completes, the page shows a one-time
   **"Connect your AI assistant"** card: your tenant id + a **read-only
   token** (30-day expiry). Click "Copy setup message" and paste it into
   your assistant. That's it — your agent now reads your records, and only
   reads: the token is cryptographically scoped so it can never write.
4. Records stream in over the next 5–45 minutes.

Treat the token like a password. Lost or expired tokens: email
support@healthclaw.io for a re-issue (tokens are minted only once per
connection, so a lost token is a support request, not a reconnect).

Other sources, same guardrails: **Health Bank One / HealthEx / MEDENT**
(OAuth pulls with in-process PHI redaction — see `scripts/`), and
**Apple Health / Fitbit** wearables sync.

**If you are recording videos: stay on the synthetic demo tenant.** Never
film real PHI, including your own.
