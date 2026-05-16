# HealthClaw on Hermes

Run the HealthClaw guardrail stack inside [Hermes](https://github.com/nousresearch/hermes-agent), Nous Research's self-improving agent. Same goals as the [`openclaw/`](../openclaw/) integration — natural-language chat with your clinical data behind PHI redaction + audit + step-up + human-in-the-loop — with Hermes' learning loop on top so the skills get better as you use them.

## Why Hermes alongside OpenClaw

| | OpenClaw | Hermes |
|---|---|---|
| Conversational gateway | Telegram bot (`openclaw/bot.py`) | Telegram · Discord · Slack · WhatsApp · Signal · CLI · HTTP |
| Skills format | Per-persona `AGENTS.md` files (Sally / Mary / Dom / Kristy) | [agentskills.io](https://agentskills.io) standard (`SKILL.md` frontmatter) |
| Persona format | `AGENTS.md` per workspace | `SOUL.md` per persona |
| Execution backend | Local Python / Docker | Local · Docker · SSH · Singularity · Modal · Daytona · Vercel Sandbox |
| Self-improvement | None — fixed slash commands | Built-in learning loop; creates skills from experience, searches past conversations |
| MCP support | HTTP bridge (`/mcp/rpc`) via JSON-RPC 2.0 | Native MCP client over Streamable HTTP |
| Skills HealthClaw ships with | 9 (curatr, fhir-r6-guardrails, phi-redaction, …) | Same 9 + this one (`skills/hermes/`) |

The HealthClaw MCP server is the same in both cases — Hermes just connects to it as a native MCP client, no JSON-RPC bridge needed.

## Quick start (~2 minutes)

**1. Install Hermes** (one-time, follow the upstream instructions):

```bash
# from https://github.com/nousresearch/hermes-agent
hermes init
```

**2. Wire HealthClaw into Hermes:**

```bash
./hermes/install.sh
```

That script:
- copies all of `skills/` into `~/.hermes/skills/healthclaw/` so Hermes can read, fork, and improve them over time
- installs the `SOUL.md` persona at `~/.hermes/personas/healthclaw.md`
- merges `hermes/mcp.json` into `~/.hermes/config.json` (idempotent — re-run any time)

**3. Start a Hermes session:**

```bash
hermes
> /persona healthclaw
> /mcp list                  # confirms healthclaw-hosted is connected
> show me my conditions
```

The hosted server is seeded with the synthetic Grover Keeling sample so you can poke around without standing up the stack locally. To use your own data, see *Local mode* and *SHARP-on-MCP mode* below.

## Three modes

### Hosted demo (default)

Uses `https://mcp-server-production-5112.up.railway.app/mcp` with tenant `desktop-demo`. Synthetic data only. No setup beyond the installer. This is what the installer turns on by default.

### Local mode

For your own data on your own machine. Start the HealthClaw stack first:

```bash
docker-compose up -d --build
curl http://localhost:5000/r6/fhir/health
```

Then in `~/.hermes/config.json`, rename the `_healthclaw-local` entry to `healthclaw-local` (drop the leading underscore) and re-launch Hermes. The persona will route to your local stack.

### SHARP-on-MCP mode

For agents that already hold a SMART-on-FHIR access token (e.g. launched from an Epic/Cerner patient portal). HealthClaw forwards the token to whatever upstream FHIR server you point at via `X-FHIR-Server-URL` and applies the guardrail stack on every response. In `~/.hermes/config.json`, rename `_healthclaw-sharp` → `healthclaw-sharp`, fill in your token + patient ID, and you're done. See [`hermes/mcp.json`](mcp.json) for the exact header shape.

This is the same SHARP-on-MCP contract that PromptOpinion's marketplace uses — both ecosystems converge on the same headers (`X-FHIR-Server-URL` / `X-FHIR-Access-Token` / `X-Patient-ID`).

## What "learns and iterates over time" actually means

Hermes captures every conversation. When a skill works well, you can save the working pattern as a new skill via `/skill save`. When a skill goes wrong, you can ask Hermes to refine the underlying SKILL.md. The skills shipped in `skills/` are starting points; Hermes treats them as the seed of your library, not the final form.

A few examples of the kinds of refinements Hermes tends to land on as you use HealthClaw:

- **`curatr-evaluate` skill**: starts as a single-shot "scan everything" call. After Hermes watches you cherry-pick specific issue types a few times, it tends to add a `categories=[…]` arg pattern.
- **`personal-health-records` skill**: starts assuming HealthEx is the inbound pipe. If Hermes sees you frequently use Flexpa or a TEFCA IAS service instead, it learns to ask which pipe before pulling.
- **`getting-started` skill**: starts assuming OpenClaw as the gateway. If you only ever use Hermes, the skill quietly stops mentioning OpenClaw.

Your `~/.hermes/skills/healthclaw/` directory drifts toward your usage pattern. The repo copy stays as the canonical starting point — `./hermes/install.sh --skills-only` refreshes from the repo without touching anything else.

## What's where

| File | What it is |
|---|---|
| [`SOUL.md`](SOUL.md) | The HealthClaw persona for Hermes. Loaded as `/persona healthclaw`. |
| [`mcp.json`](mcp.json) | MCP server config fragment. Merged into `~/.hermes/config.json` by the installer. Has three entries: hosted (default), local, SHARP-on-MCP. |
| [`install.sh`](install.sh) | Idempotent wiring script. `--dry-run` to preview, `--skills-only` to refresh just the skills. |
| `../skills/` | The shared skills library. All 10 skills are copied into `~/.hermes/skills/healthclaw/` by the installer. |
| `../openclaw/` | The parallel OpenClaw (Telegram) integration. You can run both — they share the same MCP server. |

## Migrating from OpenClaw

Hermes has built-in OpenClaw migration support: skills imported from an existing OpenClaw install land in `~/.hermes/skills/openclaw-imports/`. The HealthClaw install drops parallel skills in `~/.hermes/skills/healthclaw/`. They coexist — pick whichever you reach for first. Hermes will surface both when you ask a question that matches either.

## Troubleshooting

| Symptom | Probable cause | Fix |
|---|---|---|
| `/mcp list` shows healthclaw-hosted as disconnected | Outbound HTTPS blocked, or Railway cold start in flight | `curl https://mcp-server-production-5112.up.railway.app/health` from the same shell. If that times out, network. If 200 quickly, retry in 30s. |
| Tool calls return `{"error": "X-Tenant-Id header is required"}` | Tenant header not forwarded | Confirm `headers.X-Tenant-Id` is set in `~/.hermes/config.json` for the active server. Hosted mode defaults to `desktop-demo`. |
| Write tools 401 with `requires_step_up: true` | No step-up token in the request | Have Hermes call `fhir_get_token` first; pass the returned token as `_stepUpToken` in the next tool call. The SOUL persona handles this for you. |
| Write tools 428 | Human-in-the-loop gate didn't see `X-Human-Confirmed: true` | The SOUL persona asks you to confirm before commit; say *"yes confirm"* and retry. |
| Skills changed in `~/.hermes/skills/healthclaw/` and you want to start over | Hermes' learning loop edited the seed skills | `./hermes/install.sh --skills-only` overwrites with the repo copy. |

## See also

- [`../openclaw/`](../openclaw/) — the Telegram-bot integration (still the right choice if you only want one chat gateway and zero Hermes install)
- [`../skills/`](../skills/) — shared skills library
- [`../CLAUDE.md`](../CLAUDE.md) — full repo guide
- [Hermes upstream](https://github.com/nousresearch/hermes-agent)
- [agentskills.io](https://agentskills.io) — the open skills standard Hermes uses
