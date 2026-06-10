# Health Bank One — Post-Call Notes

**Call date:** 2026-06-10
**Participants:** Bo Holland (HBO CEO), Jason Choe (HBO DevRel), Eugene Vestel (HealthClaw)
**Purpose:** MCP onboarding + integration planning

---

## What We Learned

### Platform architecture

HBO is an **open banking system for health** — the mental model Bo Holland uses internally.
Consumers own their health + identity data in a single consolidated account. A banking-grade
digital ID enables SSO across all authorized AI applications.

**Data acquisition pipeline — two channels running in parallel:**

| Channel | Volume / Method | Notes |
|---|---|---|
| **Digital** | FHIR endpoints, patient portal scrapers | ~50% of data; 12–14% success rate today |
| **Analog (fax)** | 9 billion fax pages/year | Universal fallback; treated as legal subpoenas — providers cannot refuse or overcharge |

Goal is 100% data acquisition intent: no provider is left behind because of digital gaps.

**Data normalization:** every record — regardless of format or source — is normalized to an
AI-ready format before appearing in the MCP server. Partners never see raw fax images.

**MCP server + OAuth:**
- MCP: `https://mcp.app.healthbankone.com/mcp`
- OAuth issuer: `https://oauth.app.healthbankone.com`
- Authorization endpoint: `https://oauth.app.healthbankone.com/authorize`
- Token endpoint: `https://oauth.app.healthbankone.com/token`
- Registration endpoint: `https://oauth.app.healthbankone.com/register` (DCR / RFC 7591)
- Revocation endpoint: `https://oauth.app.healthbankone.com/revoke`
- Auth model: public client, PKCE S256, no client_secret for self-access Bootstrap tier

**REST event API:** partners can subscribe to patient data change events and automate
workflows in near real-time. Bo Holland's example: "Gene's agent gets notified when
new records arrive and automatically fills a form."

**Multi-agent authorization:** each AI agent gets cryptographically signed authorization
scoped to defined actions and time periods. The consumer approves each agent individually
and can revoke in real time.

### Roadmap items confirmed

| Timeline | Feature |
|---|---|
| Short-term | Manual write-back: patients/agents push data (device readings, corrections) into HBO account |
| Short-term | Digital identity qualification improvements |
| Next quarter | TEFCA QHIN CSP certification (currently 12–14% digital retrieval success) |
| Long-term | Continuous device data streaming with full provenance |
| Long-term | Eliminate all forms + passwords — OAuth + MCP as universal identity infrastructure |

---

## Strategic Alignment with HealthClaw

### 1. Curatr ↔ HBO dispute resolution

Bo Holland explicitly said healthcare has **no equivalent of financial services dispute resolution** — no way for patients to formally flag data errors back to providers.

Curatr is the missing piece. The flow Bo and Gene agreed to explore:

```
Curatr finds discrepancy in HBO records
  → generates correction letter (PDF)
  → patient approves in Telegram (/approve)
  → letter sent to provider (via HBO fax pipeline or direct)
  → provider corrects record
  → HBO receives updated FHIR resource
  → Curatr re-evaluates → quality score improves
```

This is the **feedback loop neither HealthEx nor Fasten offers**. HBO's fax pipeline
means it works even for providers with zero digital infrastructure.

### 2. Form auto-fill demo (Bo Holland's strongest example)

Gene's form-fill use case resonated most strongly: take a 40-page intake form URL,
HealthClaw queries HBO data via MCP, populates every field, submits on the patient's
behalf, sends confirmation.

**Why this works as a demo:**
- Every attendee has filled out a 40-page intake form
- The friction is universally hated
- The demo is instant and visual
- Shows the full stack: HBO data → HealthClaw agent → form submitted → audit trail

### 3. Multi-agent authorization

Gene's vision of specialized agents (Sally-PCP, Mary-pharmacy, Dom-fitness, Kristy-scheduler)
maps directly to Bo Holland's cryptographic agent delegation model. Each Telegram persona
gets a scoped OAuth grant from HBO; HealthClaw's step-up + HITL pattern handles write authorization.

### 4. HealthClaw as the "operating system layer"

Gene's framing: HealthClaw is the OS between HBO (data source) and AI agents (consumers).
HBO provides data + identity; HealthClaw provides guardrails + audit + Curatr + Telegram UX.
Bo Holland endorsed this layered model.

---

## Action Items

### Gene (by Sunday 2026-06-15 — before flight)

- [x] Connect to HBO MCP server (`.mcp.json` updated, `healthbankone_oauth.py` ready)
- [ ] **Demo prep:** build the form-fill demo (see `docs/dev-days-demo-runbook.md`)
- [ ] **Authorize live:** run `python scripts/healthbankone_oauth.py authorize` + scan QR with HBO app
- [ ] **Discover tools:** `python scripts/export_healthbankone_mcp.py --discover --tenant-id ev-personal-hbo`
- [ ] Share demo recording with Bo Holland + Jason Choe before Sunday

### HBO (Bo Holland)

- [ ] Enable write-back for Curatr-generated corrections
- [ ] Explore feedback loop / dispute resolution API with Gene
- [ ] Collaborate on device data streaming integration

### HBO (Jason Choe)

- [ ] Technical onboarding support via Slack / `developer@healthbankone.com`
- [ ] Share any additional developer docs or webhook API spec

---

## Questions Still Open

1. **Event notification spec:** What does the REST event API look like? Webhook URL format? Event types (new_record, record_updated)? Signing (Standard-Webhooks HMAC like Fasten)?
2. **Write-back scope:** Can Curatr fixes be written back directly, or does the patient need to confirm in the HBO app first?
3. **Tool catalog:** What tools does the MCP server expose? (Will discover via `--discover`)
4. **Scopes:** What OAuth scopes exist beyond `openid offline_access`? Is there a `data.read`, `data.write`, `identity.read`?
5. **TEFCA timing:** Once they're a CSP, does the HBO pull replace the need for Fasten TEFCA connect for some patients?

---

## Notes on the Bootstrap Program

- Self-access (your own records): free, no approval needed, no client_secret
- Multi-patient access: requires commercial agreement via `developer@healthbankone.com`
- Sandbox / synthetic data: **coming soon** (not yet available — use real account for now)
- Developer support: Jason Choe, Slack or email

---

## Key Quote

> "With one connection, users would never need to manually fill out forms or remember
> passwords again." — Bo Holland

This is the HealthClaw + HBO pitch in one sentence.
