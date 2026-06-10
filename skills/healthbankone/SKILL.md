---
name: healthbankone
description: >
  Pull verified medical records and digital-identity context from Health Bank
  One (https://www.healthbankone.com) via their MCP server. HBO uses OAuth 2.x
  with per-consumer authorization, so this skill drives an authorization-code
  grant first, then runs the MCP pull, then ingests the redacted bundle into a
  HealthClaw tenant. Use when the user wants to add HBO as a source alongside
  HealthEx and Fasten Connect. Triggers on prompts like "connect Health Bank
  One", "pull from HBO", "verify identity through Health Bank One".
version: 0.2.0
author: Eugene Vestel (fhiriq.com)
license: MIT
status: live — MCP endpoint confirmed 2026-06-10 (Bootstrap Developer Program)
references:
  hbo_home: https://www.healthbankone.com
  hbo_developer_program: https://www.healthbankone.com/MCP
  hbo_launch_announcement: https://www.prweb.com/releases/health-bank-one-gives-ai-applications-access-to-trusted-digital-identity-and-verified-medical-records-through-mcp-302770638.html
  call_prep: https://github.com/aks129/HealthClawGuardrails/blob/main/docs/healthbankone-call-prep.md
  healthex_skill: https://github.com/aks129/HealthClawGuardrails/tree/main/skills/healthex-export-redacted
  fasten_skill: https://github.com/aks129/HealthClawGuardrails/tree/main/skills/fasten-connect
---

# Health Bank One — pull verified records via MCP

> **Status:** live. Bootstrap Developer Program onboarded 2026-06-10.
>
> **MCP endpoint:** `https://mcp.app.healthbankone.com/mcp`
>
> **Self-access auth:** OAuth via browser/QR code — no `client_secret` needed for
> your own records. Claude Code: add to `.mcp.json` and run `/mcp` to authorize.
> Multi-patient (commercial) auth uses Open Dynamic Client Registration (RFC 7591).

HBO sits in our health-data source matrix as the **OAuth-pulled, identity-verified** equivalent of:

- **HealthEx** (token-pulled, no per-consumer consent — see `healthex-export-redacted`)
- **Fasten Connect** (webhook-pushed, TEFCA-verified — see `fasten-connect`)

## What HBO gives us that HealthEx and Fasten don't

- **Digital Identity verification baked in.** IAL2 / AAL2 / PSD2-grade. Once a consumer authorizes HealthClaw, we get identity attributes alongside the clinical bundle — no separate CLEAR / ID.me step.
- **Paper-record retrieval.** HBO's pipeline includes mail-based requests for records that aren't yet digital. Useful for older patients with significant pre-2015 history.
- **Writebacks.** The Engagement service exposes authorized writebacks; HealthClaw can publish curatr fixes or annotated documents back to the consumer's HBO account.
- **Insurance Context.** Verified payer details — possibly the strongest case for HBO over the other two sources.

## Setup

### 1. Self-access (Bootstrap — your own records)

**Claude Code** — add to project `.mcp.json` (already done in this repo):

```json
"healthbankone": {
  "type": "http",
  "url": "https://mcp.app.healthbankone.com/mcp"
}
```

Then in a new session run `/mcp` → browser opens with QR code → scan with the
Health Bank One digital ID app → approve → connected.

**Claude Desktop** — `+ → Connectors → Manage Connectors → Add custom connector`
→ enter `https://mcp.app.healthbankone.com/mcp` → Connect → scan QR.

**Script pull** (for export → redact → ingest pipeline):

```bash
export HBO_MCP_URL=https://mcp.app.healthbankone.com/mcp
# Authorize once (opens browser + QR):
python scripts/healthbankone_oauth.py authorize --tenant-id ev-personal-hbo
# Then pull + redact + ingest:
python scripts/export_healthbankone_mcp.py --tenant-id ev-personal-hbo --discover
```

### 2. Multi-patient access (Commercial license required)

Uses **Open Dynamic Client Registration** (RFC 7591) to obtain `client_id` +
`client_secret`. Then standard authorization-code + PKCE per patient. Contact
`developer@healthbankone.com` to start a commercial conversation.

For the HealthClaw pipeline:

1. Register via DCR at the HBO registration endpoint (URL TBD)
2. Store `HBO_CLIENT_ID`, `HBO_CLIENT_SECRET` on Railway HealthClawGuardrails service
3. `python scripts/healthbankone_oauth.py authorize --tenant-id <patient-tenant>`
   — opens authorize URL; callback at `https://app.healthclaw.io/hbo/callback`
4. Tokens cached in `~/.healthclaw/hbo_tokens.json` (local) or Redis (Railway)

### 3. Pull the records (script pipeline)

```bash
python scripts/export_healthbankone_mcp.py \
  --tenant-id ev-personal-hbo \
  --output ~/.healthclaw/exports/hbo-$(date +%Y-%m-%d).json
```

What the script does:

1. Loads access token from cache (`~/.healthclaw/hbo_tokens.json`); refreshes if expired
2. Opens MCP Streamable HTTP session to `https://mcp.app.healthbankone.com/mcp` with `Authorization: Bearer <token>`
3. `--discover` mode: calls `tools/list`, invokes every read-safe tool (filters on `readOnlyHint` annotation + name heuristics)
4. Redacts PHI in-process via `scripts/healthclaw_redact.py` — raw response never touches disk
5. Writes the redacted snapshot to disk

### 4. Ingest into HealthClaw

```bash
python scripts/import_healthex.py \
  --bundle-file ~/.healthclaw/exports/hbo-2026-06-04.json \
  --tenant-id ev-personal-hbo \
  --step-up-secret "$STEP_UP_SECRET"
```

The `import_healthex.py` script is source-agnostic — it just POSTs a FHIR Bundle to `/Bundle/$ingest-context`. Works for HBO output unchanged.

## OpenClaw slash commands

| Command | What it does |
|---|---|
| `/hbo_connect` | Builds the OAuth authorization URL (PKCE S256); user opens link, logs in, grants; tokens cached |
| `/hbo_pull` | Runs the export + redact + ingest pipeline in background; pings Telegram when records arrive |

(Implemented in `openclaw/bot.py` and `scripts/bot_commands.py`.)

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `HBO_MCP_URL` | Yes | `https://mcp.app.healthbankone.com/mcp` |
| `HBO_CLIENT_ID` | Commercial only | From HBO DCR registration |
| `HBO_CLIENT_SECRET` | Commercial only | Same |
| `HBO_AUTHORIZATION_ENDPOINT` | Commercial only | From HBO DCR metadata |
| `HBO_TOKEN_ENDPOINT` | Commercial only | From HBO DCR metadata |
| `HBO_REDIRECT_URI` | Commercial only | Default: `https://app.healthclaw.io/hbo/callback` |
| `HBO_SCOPES` | Optional | Space-separated; default: `openid offline_access` |

## SHARP-on-MCP compatibility check

If HBO's MCP server advertises SHARP (`capabilities.experimental.fhir_context_required`) or the PromptOpinion FHIR Extension, HealthClaw can also act as a *forwarding* layer — an MCP client that pulls from HBO using SHARP headers on every call instead of pre-pulling a snapshot. This eliminates the export-to-disk step entirely and matches the pattern PromptOpinion uses with us today. **Ask on the call whether they advertise either spec.** If yes, we can offer to demo HealthClaw + HBO as a SHARP-compliant pair.

## Comparison to existing skills

| Aspect | HealthEx | Fasten Connect | Health Bank One |
|---|---|---|---|
| Source skill | `healthex-export-redacted` | `fasten-connect` | `healthbankone` (this one) |
| Auth | Bearer token in env | Stitch widget public key + webhook HMAC | OAuth 2.x per-consumer |
| Identity verification | Done by HealthEx | CLEAR / ID.me via TEFCA | HBO Digital Identity (IAL2/AAL2) |
| Transport | MCP Streamable HTTP pull | Webhook push | MCP Streamable HTTP pull |
| Data freshness | On-demand via `update_records` | Push on EHR change | On-demand via pull (refresh cadence TBD) |
| FHIR format | R4 + US Core | R4 NDJSON | R4 (per their materials) |
| Writebacks | No | No | Yes — Engagement service |
| Pricing | Free / paid tiers | Paid keys (test_ / live_) | Bootstrap free, post-launch TBD |
