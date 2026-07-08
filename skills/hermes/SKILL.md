---
name: hermes
description: >
  Run the HealthClaw guardrail stack inside Hermes, Nous Research's
  self-improving AI agent. Same conversational gateway and same compliance
  goals as the OpenClaw integration, but with Hermes' learning loop — skills
  that improve from experience, conversation memory across sessions, and
  native MCP support over Streamable HTTP. Triggers when a user asks about
  Hermes, the agentskills.io standard, SOUL personas, the Nous agent, or
  wants an alternative to OpenClaw that learns over time.
version: 1.0.0
author: HealthClaw contributors
license: MIT
references:
  hermes_repo: https://github.com/nousresearch/hermes-agent
  agentskills: https://agentskills.io
  healthclaw_repo: https://github.com/aks129/HealthClawGuardrails
  openclaw_integration: https://github.com/aks129/HealthClawGuardrails/tree/main/openclaw
  hermes_integration: https://github.com/aks129/HealthClawGuardrails/tree/main/hermes
  mcp_endpoint: https://mcp-server-production-5112.up.railway.app/mcp
---

# Hermes — self-improving HealthClaw gateway

HealthClaw works as a Hermes agent the same way it works as an OpenClaw bot: a SOUL persona on top of the same MCP server with the same guardrails. The difference is Hermes treats every conversation as training data for its skill library, so the integration gets better the more you use it.

## When to reach for Hermes instead of OpenClaw

| You want… | Use |
|---|---|
| One Telegram bot with fixed slash commands you trust | OpenClaw |
| Multi-gateway chat — Telegram + Discord + Slack + Signal + WhatsApp + CLI | Hermes |
| Skills that learn from your past conversations | Hermes |
| To run agents on Modal / Daytona / Vercel Sandbox / SSH | Hermes |
| Native MCP client (no JSON-RPC HTTP bridge) | Hermes |
| Already invested in `AGENTS.md` + persona workspaces | OpenClaw |

They share the same MCP server, the same skills library, the same guardrails — so running both is fine. The HealthClaw install for Hermes lands skills at `~/.hermes/skills/healthclaw/`, separate from any OpenClaw migration imports at `~/.hermes/skills/openclaw-imports/`.

## Setup (~2 minutes)

### 1. Install Hermes

Follow the upstream instructions at [github.com/nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent#installation). At the end you should have a `hermes` CLI on PATH and a `~/.hermes/` directory.

### 2. Wire HealthClaw

From the HealthClaw repo root:

```bash
./hermes/install.sh
```

Idempotent — re-run any time. Flags:

- `--dry-run` shows what it would do without touching anything
- `--skills-only` refreshes just the skills (use after you've edited them and want the repo copy back)

The installer:
- copies every skill in `skills/` to `~/.hermes/skills/healthclaw/`
- installs the `SOUL.md` persona at `~/.hermes/personas/healthclaw.md`
- merges three MCP server entries (hosted, local, SHARP) into `~/.hermes/config.json`

### 3. Use it

```bash
hermes
> /persona healthclaw
> /mcp list                       # confirms healthclaw-hosted connected
> show me my conditions
> what's my last HbA1c?
> propose adding a new allergy: peanut, moderate severity
```

The SOUL persona handles tool selection, step-up tokens, and the human-in-the-loop confirm step. You just type what you want.

## The three modes

The bundled `hermes/mcp.json` ships three server entries; the installer enables the hosted demo by default and leaves the other two commented out (`_` prefix on the key). Rename the keys to enable.

| Key | Use case | Setup |
|---|---|---|
| `healthclaw-hosted` | Try everything against the synthetic Grover Keeling sample data. | Default — installer enables. |
| `healthclaw-local` | Your own data on your own machine. | `docker-compose up -d --build`, then rename `_healthclaw-local` → `healthclaw-local` in config. |
| `healthclaw-sharp` | SMART-launched agent forwards its FHIR access token to HealthClaw, which guards a real EHR. | Rename `_healthclaw-sharp` → `healthclaw-sharp`, fill in `X-FHIR-Access-Token` + `X-Patient-ID`. |

The SHARP mode is the same `X-FHIR-Server-URL` / `X-FHIR-Access-Token` / `X-Patient-ID` header contract that PromptOpinion's marketplace uses, so a single deployment works against Epic, Cerner, MEDITECH, athenahealth, eClinicalWorks, HAPI, SMART Health IT — no per-EHR config.

## How the learning loop helps

Hermes captures every conversation and builds memory across sessions. When a skill works, save the working pattern via `/skill save`. When a skill goes wrong, ask Hermes to refine it. The HealthClaw skills you install are starting points — Hermes treats them as the seed of your library, not the final form.

Examples of refinements that tend to emerge as you use HealthClaw on Hermes:

- The `curatr-evaluate` skill starts as a broad scan. After Hermes watches you cherry-pick specific issue types ("just smoking-status contradictions"), it adds a `categories` arg pattern.
- The `personal-health-records` skill starts assuming HealthEx as the inbound pipe. If you use Flexpa or a TEFCA IAS service instead, it learns to ask which pipe before pulling.
- Bot conversations that ended in step-up rejection tend to grow a `/approve` prompt at the right moment.

Your `~/.hermes/skills/healthclaw/` directory drifts toward your usage pattern. The repo copy stays canonical — `./hermes/install.sh --skills-only` refreshes from the repo if you want to start over.

## What the SOUL persona does for you

The shipped [`hermes/SOUL.md`](https://github.com/aks129/HealthClawGuardrails/blob/main/hermes/SOUL.md) handles:

- Tool selection (read vs. write vs. utility)
- Step-up token acquisition before any write
- Human-in-the-loop confirm prompts before commit
- Narration of what the guardrails did to each response (so redactions don't feel like bugs)
- Once-per-session medical disclaimer on clinical resources
- Refusal to claim demo data is real, or to invent FHIR resource types

You can edit `~/.hermes/personas/healthclaw.md` and Hermes will pick up the change next session. To revert, re-run the installer.

## Reusing OpenClaw personas (Sally / Mary / Dom / Kristy)

If you already have OpenClaw personas configured, Hermes' built-in OpenClaw migration drops imported skills at `~/.hermes/skills/openclaw-imports/`. The HealthClaw install drops parallel skills at `~/.hermes/skills/healthclaw/`. Both are visible to every Hermes session — the agent picks whichever matches the user's intent.

You can also load an OpenClaw `AGENTS.md` directly as a Hermes persona:

```bash
cp ~/.healthclaw/sally/AGENTS.md ~/.hermes/personas/sally-pcp.md
hermes
> /persona sally-pcp
```

Sally's slash commands map to Hermes natural-language equivalents because both call the same MCP tools underneath.

## Troubleshooting

Mirrors the OpenClaw troubleshooting matrix. The most common Hermes-specific issue:

| Symptom | Fix |
|---|---|
| `/mcp list` shows healthclaw-hosted as disconnected | Outbound HTTPS blocked, or Railway cold start. `curl https://mcp-server-production-5112.up.railway.app/health` — if 200, retry. |
| Hermes refuses to load `SOUL.md` | Check `~/.hermes/personas/healthclaw.md` exists and is readable. Re-run `./hermes/install.sh`. |
| Skills changed unexpectedly | `./hermes/install.sh --skills-only` restores the repo copy. |

## See also

- [getting-started](../getting-started/SKILL.md) — full stack onboarding (assumes OpenClaw; substitute Hermes anywhere it says OpenClaw)
- [personal-health-records](../personal-health-records/SKILL.md) — pulling and analyzing your own EHR data
- [curatr](../curatr/SKILL.md) — clinical data quality engine
- [fhir-upstream-proxy](../fhir-upstream-proxy/SKILL.md) — using HealthClaw in front of a real FHIR server (the SHARP mode story)
