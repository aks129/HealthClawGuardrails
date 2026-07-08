---
name: getting-started
description: >
  End-to-end onboarding for the HealthClaw + OpenClaw personal-health-agent
  stack. Walks anyone — not just developers — through (1) installing OpenClaw
  as the local AI gateway, (2) standing up an open-source FHIR server (HAPI or
  Medplum) on their machine, (3) connecting their EHR records via HealthEx,
  Josh Mandel's fhir-skills, Flexpa, or any patient-right-of-access / TEFCA IAS
  service, (4) installing HealthClaw Guardrails with the OpenClaw personas
  pre-wired, and (5) pulling + reviewing health data end-to-end. Triggers when
  a user asks "how do I get started", "how do I set this up", "what do I need
  to use HealthClaw", "first time setup", "install everything", or any first-run
  question. Use this before any other HealthClaw skill when the user hasn't yet
  built their stack.
version: 1.0.0
license: FSL-1.1-MIT
references:
  openclaw_docs: https://docs.openclaw.ai
  openclaw_home: https://openclaw.ai
  healthclaw_repo: https://github.com/aks129/HealthClawGuardrails
  hapi_fhir: https://github.com/hapifhir/hapi-fhir-jpaserver-starter
  hapi_demo: https://hapi.fhir.org
  medplum: https://github.com/medplum/medplum
  medplum_docs: https://www.medplum.com/docs
  healthex: https://healthex.io
  fhir_skills: https://github.com/jmandel/fhir-skills
  flexpa: https://www.flexpa.com
  smart_app_launch: https://hl7.org/fhir/smart-app-launch
  patient_right_of_access: https://www.healthit.gov/topic/patients-families/right-access
---

# Getting started — HealthClaw + OpenClaw + your health data

The goal: a fully local, private, agent-mediated view of your own clinical
records. Nothing leaves your machine unless you explicitly send it. By the
end of this guide you'll have:

- An **OpenClaw gateway** (your local AI assistant runtime — Telegram /
  WhatsApp / iMessage / Slack / web)
- An **open-source FHIR server** holding your records (HAPI or Medplum)
- Your real **EHR records** pulled in via a patient-right-of-access
  service of your choice
- **HealthClaw Guardrails** sitting between any agent and that data,
  enforcing PHI redaction, audit trails, step-up auth, and tenant isolation
- **OpenClaw agent personas** (Sally-PCP, Mary-pharmacy, Dom-fitness,
  Kristy-scheduler) already wired to the slash-command surface and aware
  of every HealthClaw tool

If you already have one or more of these, the guide will tell you how to
verify and skip ahead — don't reinstall what's working.

> **Privacy guarantee.** Steps 1–4 keep your raw clinical data on your
> machine. The only network calls are to your chosen EHR connector
> (HealthEx, Flexpa, etc.) and that traffic is OAuth/SMART-on-FHIR with
> your explicit consent. PHI is redacted **in-process** before any file
> is written.

---

## Prerequisites

| Requirement | Why | How to check |
| --- | --- | --- |
| macOS / Linux | Stack is tested on Darwin + Linux. Windows works under WSL2. | `uname -sm` |
| Python 3.11+ | Flask app + scripts | `python3 --version` |
| Node 22+ | OpenClaw + the MCP orchestrator | `node --version` |
| Docker (optional) | Easiest way to run HAPI / Medplum / HealthClaw | `docker --version` |
| `git` | All repos | `git --version` |
| ~30 min | First-time setup | — |

If anything is missing, install via Homebrew (macOS) or your distro's
package manager. Don't proceed until all four exist.

---

## Step 1a — OpenClaw

OpenClaw is the local AI gateway: it runs your agent personas, exposes them
on whichever channels you want (Telegram, WhatsApp, iMessage, Slack, web),
and gives them access to your installed skills.

### Check whether it's already installed

```bash
which openclaw                     # binary path, if present
openclaw --version                 # confirms install
ls ~/.openclaw 2>/dev/null         # workspace dir
launchctl list | grep openclaw     # macOS LaunchAgent (if running as daemon)
```

If `openclaw --version` prints a version and `~/.openclaw` exists, **skip to
Step 1b**. Otherwise continue.

### Install it

The canonical install lives at **<https://openclaw.ai>** — start there. The
homepage points to the current install method (npm vs Homebrew vs scripted
installer); use whatever it tells you, since the install path can change
between releases. The historical commands the repo has used:

```bash
# A — npm (most common)
npx -y @openclaw/cli init

# B — Homebrew tap
brew install openclaw/tap/openclaw

# C — scripted installer (check openclaw.ai for the current curl URL)
```

After install, log in and start the gateway:

```bash
openclaw auth login                # OAuth in browser
openclaw gateway start             # http://localhost:4319 by default
openclaw status                    # confirm "running"
```

For an always-on Mac mini setup (LaunchAgent so it survives reboot), see
[`docs/mac-mini-setup.md`](docs/mac-mini-setup.md) in this repo — it has the
full plist + `caffeinate` recipe.

### Persona workspaces

OpenClaw runs each persona as a separate workspace under
`~/.openclaw/workspace/`. The HealthClaw repo provides a one-shot script to
seed Sally / Mary / Dom / Kristy:

```bash
# (run after cloning HealthClaw — covered in Step 3)
./scripts/seed_openclaw_workspaces.sh
```

Don't run this yet — Step 3 will tell you when.

---

## Step 1b — Open-source FHIR server

You need somewhere to **store** your records once you pull them. Two solid
choices, pick one:

### Option A — HAPI FHIR (recommended for first-timers)

The reference Java implementation. Easiest to run, supports R4 + R5, no
auth out of the box (which is fine on `localhost`).

```bash
# Already running anywhere?
curl -sf http://localhost:8080/fhir/metadata >/dev/null && echo "HAPI is up"

# If not — Docker is the fastest path
docker run -d --name hapi-fhir \
  -p 8080:8080 \
  hapiproject/hapi:latest

# Wait ~30s, then verify
curl -s http://localhost:8080/fhir/metadata | head -c 200
```

You now have a FHIR R4 server at `http://localhost:8080/fhir`.

Public sandbox (zero install, but **don't put real data here** — it's shared
and wiped weekly): <https://hapi.fhir.org/baseR4>.

Repo: <https://github.com/hapifhir/hapi-fhir-jpaserver-starter>.

### Option B — Medplum (more features, OAuth-ready)

Full Medplum platform: FHIR R4 + auth + dashboard + audit. Heavier setup,
but gives you a UI to browse resources.

```bash
git clone https://github.com/medplum/medplum
cd medplum
docker compose -f compose-dev.yaml up -d
# Wait 60–90s for Postgres + server to come up
open http://localhost:3000   # admin UI
```

Then create a project, copy the project ID, and set
`MEDPLUM_BASE_URL=https://api.medplum.com/fhir/R4/<project-id>` (or your
local URL) — HealthClaw's upstream proxy talks to it via OAuth2
client-credentials.

Repo: <https://github.com/medplum/medplum> · Docs:
<https://www.medplum.com/docs>.

### Verify before continuing

```bash
# Whichever you picked, this should print FHIR capability statement
curl -s http://localhost:8080/fhir/metadata | python3 -c \
  "import sys, json; d=json.load(sys.stdin); \
   print('FHIR', d.get('fhirVersion'), '·', d.get('software',{}).get('name'))"
```

Don't proceed without a green response — Steps 3+ assume one is reachable.

---

## Step 2 — Connect your real records

This is the "patient right of access" step — you authenticate with your
healthcare provider(s) and pull your records into the FHIR server you just
set up. **Records never go to the cloud.** They flow EHR → connector →
your machine, and stop there.

### Have you already connected somewhere?

Quick checks for the common services:

```bash
# HealthEx (claude.ai integration)
# — check claude.ai → Settings → Integrations → HealthEx → "Connected"?

# Local export bundles already on disk?
ls -1t exports/healthex-*.json ~/.healthclaw/exports/healthex-*.json 2>/dev/null | head -3

# A FHIR server already populated?
curl -s "http://localhost:8080/fhir/Patient?_count=1" | grep -c '"resourceType":"Patient"'
```

If any of those return existing data, **skip to Step 3** — you're already
connected.

### Pick a connector

| Service | Best for | What it gives you | Auth |
| --- | --- | --- | --- |
| **[HealthEx](https://healthex.io)** | US patients with Epic / Cerner / CommonWell / Carequality | Pre-built MCP tools (`get_health_summary`, `get_conditions`, `get_labs`, …) directly in claude.ai | OAuth in browser — one-time per provider |
| **[Josh Mandel's fhir-skills](https://github.com/jmandel/fhir-skills)** | DIY / SMART-on-FHIR savvy users | A skill-based bridge that lets Claude talk SMART-on-FHIR to Epic and other systems | SMART app launch you configure |
| **[Flexpa](https://www.flexpa.com)** | Apps that need structured FHIR via an API | Hosted patient-right-of-access API; you get an access token + FHIR base URL | OAuth |
| Direct SMART-on-FHIR | Power users who want zero middleware | Register a SMART app at the EHR's developer portal; auth → token → FHIR API | OAuth + PKCE |
| **TEFCA IAS** (via Fasten) | Cross-network records (multiple health systems via QHIN) | One auth → records from every health system on the QHIN network | Stitch widget (TEFCA mode) |

### Recommended path: HealthEx

Lowest friction, cleanest output, and HealthClaw already has a one-command
import that uses the official MCP SDK and redacts PHI in-process.

1. Create a HealthEx account at <https://healthex.io>
2. In claude.ai → **Settings → Integrations** → **HealthEx** → **Connect**
3. In the HealthEx UI, connect your health systems (Epic, Cerner,
   CommonWell, etc.) — each one is a SMART-on-FHIR auth flow
4. Grab your HealthEx auth token (see HealthEx docs) and store it in
   macOS Keychain so the agents can find it later:

   ```bash
   security add-generic-password -s healthex -a me -w '<your-token>'
   ```

5. Verify connection from Claude:

   > *"Check when my health records were last updated"*

   That triggers `HealthEx:update_and_check_recent_records` and confirms
   the link.

You'll do the actual data pull in **Step 4** — Step 2 is only about getting
authenticated.

### Alternative: Josh Mandel's fhir-skills

A more developer-leaning path. Clone the skill repo, configure your SMART
app credentials for whichever EHR you're targeting (Epic Open Sandbox is
free for personal use), and Claude gets SMART-on-FHIR tools.

```bash
git clone https://github.com/jmandel/fhir-skills ~/.claude/skills/fhir-skills
# Follow the README for SMART app registration and config
```

### Alternative: Flexpa

Hosted FHIR-as-a-service. Sign up at <https://www.flexpa.com>, follow their
onboarding to authorize your patient identity, and you get a FHIR base URL
+ access token. Plug those into HealthClaw's upstream proxy:

```bash
# In your .env
FHIR_UPSTREAM_URL=https://api.flexpa.com/fhir
# Add Flexpa's bearer token in your reverse-proxy / FHIR client
```

### Why none of these touch the cloud (from your point of view)

The connector talks **directly** to your EHR — your machine in the middle,
the cloud only as the OAuth provider. Once records arrive on your machine,
the next step (HealthClaw) keeps them there. If you want extra paranoia,
run everything inside Docker on a host with no outbound internet except the
EHR endpoints.

---

## Step 3 — Install HealthClaw Guardrails (with OpenClaw personas wired)

```bash
git clone https://github.com/aks129/HealthClawGuardrails
cd HealthClawGuardrails

# Install Python deps (uv is fastest — falls back to pip if you don't have it)
uv sync || pip install -e .

# Bootstrap your local secrets
cp .env.example .env
# Edit .env — at minimum set STEP_UP_SECRET to a random 32-byte hex string:
#   python3 -c "import secrets; print(secrets.token_hex(32))"
```

### Wire in your FHIR server

In `.env`, point HealthClaw at the server you stood up in Step 1b:

```
FHIR_UPSTREAM_URL=http://localhost:8080/fhir         # HAPI
# or
MEDPLUM_BASE_URL=https://api.medplum.com/fhir/R4/<project-id>
MEDPLUM_CLIENT_ID=…
MEDPLUM_CLIENT_SECRET=…
```

Leave `FHIR_UPSTREAM_URL` empty if you'd rather use HealthClaw's bundled
SQLite store for the first run.

### Start the stack

```bash
# Full stack (Flask + Redis + MCP server + Node orchestrator)
docker-compose up -d --build

# Or, lighter: just Flask
python main.py
```

Verify:

```bash
curl http://localhost:5000/r6/fhir/health
# {"status":"ok","timestamp":"…","upstream":"healthy"} or "local"
```

### Seed the OpenClaw personas

This creates four agent workspaces — Sally (PCP), Mary (pharmacy), Dom
(fitness), Kristy (scheduler) — each with an `AGENTS.md` that already lists
every HealthClaw slash command and tool:

```bash
./scripts/seed_openclaw_workspaces.sh
./scripts/update_agent_prompts.sh        # ensures each persona is current

# Install the slash-command dispatcher into ~/.healthclaw/
./scripts/bot_commands_install.sh
```

Each persona's `AGENTS.md` references `~/.healthclaw/commands.py` for the
mechanics and lists the slash commands the LLM should handle:
`/dashboard`, `/health`, `/conditions`, `/labs`, `/vitals`, `/meds`,
`/allergies`, `/immunizations`, `/summary`, `/fhir <type>`, `/export`,
`/import <path>`, `/help`, `/week`, `/conflicts`.

### Connect Claude Desktop / Claude Code

Add to your client's MCP config:

```json
{
  "mcpServers": {
    "healthclaw-local": {
      "type": "http",
      "url": "http://localhost:3001/mcp",
      "headers": { "X-Tenant-ID": "my-health" }
    }
  }
}
```

(Claude Code: `.mcp.json` in the project root. Claude Desktop:
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS.)

Restart the client and confirm the 14 HealthClaw MCP tools appear:
`fhir_search`, `fhir_read`, `fhir_validate`, `fhir_propose_write`,
`fhir_commit_write`, `curatr_evaluate`, `curatr_apply_fix`, etc.

### Cross-reference

For deeper detail on any one piece:

- [`fhir-r6-guardrails` skill](../fhir-r6-guardrails/SKILL.md) — the 14 MCP tools and their guarantees
- [`phi-redaction` skill](../phi-redaction/SKILL.md) — what gets redacted and why
- [`fhir-upstream-proxy` skill](../fhir-upstream-proxy/SKILL.md) — talking to HAPI / Medplum / Epic
- [`personal-health-records` skill](../personal-health-records/SKILL.md) — the conversational data-pull workflow

---

## Step 4 — Pull data through HealthClaw

You're now authenticated to your EHR (Step 2) and HealthClaw is running
(Step 3). Time to pipe records in.

### Option A — One command from the agent (recommended)

In Telegram (or any OpenClaw channel), DM any persona:

```
/export
```

That invokes `scripts/bot_commands.py → cmd_export()`, which:

1. Reads `HEALTHEX_AUTH_TOKEN` from env or macOS Keychain (set in Step 2)
2. Opens an MCP Streamable HTTP session to `https://api.healthex.io/mcp`
3. Calls `update_records` + `check_records_status` to refresh
4. Pulls every clinical category (`get_health_summary`, `get_conditions`,
   `get_medications`, `get_allergies`, `get_immunizations`, `get_vitals`,
   `get_labs`, `get_procedures`, `get_visits`, `search_clinical_notes`)
5. **Redacts PHI in-process** (names → initials, addresses → state+country,
   identifiers → SHA-256, birthDate → YYYY, telecom → "***") — raw response
   never touches disk
6. Writes `~/.healthclaw/exports/healthex-<date>.json`
7. Prints `_meta.redaction_stats` back to chat

Then ingest into your FHIR server (also via the agent):

```
/import ~/.healthclaw/exports/healthex-<date>.json
```

This POSTs to `/Bundle/$ingest-context` with a step-up token, validates
each entry, and writes through the guardrail proxy to your HAPI / Medplum
upstream.

### Option B — Run the scripts directly

```bash
# Export (HealthEx → redacted bundle)
HEALTHEX_AUTH_TOKEN="$(security find-generic-password -s healthex -w)" \
  python scripts/export_healthex_mcp.py \
  --tenant-id my-health \
  --output exports/healthex-$(date +%Y-%m-%d).json

# Import (bundle → your FHIR server, gated by step-up auth)
python scripts/import_healthex.py \
  --bundle-file exports/healthex-$(date +%Y-%m-%d).json \
  --tenant-id my-health \
  --step-up-secret "$STEP_UP_SECRET"
```

For Fasten Health export bundles (different shape), run
`scripts/convert_fasten.py` first to normalize.

### Option C — Pull directly via Claude conversation

If HealthEx is connected to claude.ai (Step 2), just ask:

```
Pull my complete health history across all categories going back 15 years,
fully paginated. Then build a de-identified FHIR R4 transaction bundle with
US Core resources and write it to healthclaw-bundle-<date>.json.
```

Then `/import` the resulting file. The
[`personal-health-records` skill](../personal-health-records/SKILL.md)
documents this workflow in detail.

---

## Step 5 — Verify everything works

A 60-second checklist. Each line should produce a green response.

### Liveness

```bash
# 1. OpenClaw gateway
openclaw status                           # → "running"

# 2. FHIR server
curl -sf http://localhost:8080/fhir/metadata | head -c 80

# 3. HealthClaw
curl -sf http://localhost:5000/r6/fhir/health

# 4. MCP server (only if running the Node orchestrator)
curl -sf http://localhost:3001/mcp/health || echo "(skip if not using orchestrator)"
```

### Records actually present

```bash
# Patient count in the FHIR server
curl -s "http://localhost:8080/fhir/Patient?_summary=count" \
  | python3 -c "import sys,json; print('Patients:', json.load(sys.stdin).get('total'))"

# Record breakdown via HealthClaw (PHI-redacted)
curl -s -H "X-Tenant-ID: my-health" \
  "http://localhost:5000/r6/fhir/Bundle?type=collection&_summary=count"
```

### Agent-side smoke test

In Claude Desktop / Code with the `healthclaw-local` MCP server connected:

```
Use the healthclaw-local tools. Get a step-up token, search for all
Patients in tenant my-health, then read one Condition and one Observation.
Confirm the responses include _mcp_summary and that PHI fields look
redacted.
```

You should see:
- Patient names like `"E. V."` instead of full names
- Identifiers prefixed with `redacted:sha256:`
- `_mcp_summary` on each result with reasoning + clinical context

### Telegram/OpenClaw smoke test

DM any persona on Telegram:

```
/health
/summary
/conditions
```

Each should return a structured response paraphrased by the LLM. If
`/health` reports any subsystem as **down**, fix that before moving on.

### Audit trail

```bash
# Confirm every read/write you just did was logged
curl -s -H "X-Tenant-ID: my-health" \
  "http://localhost:5000/r6/fhir/AuditEvent?_count=20&_sort=-_lastUpdated" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('AuditEvents:', d.get('total'))"
```

You should see one `AuditEvent` per resource you've touched, including a
clear actor + action + outcome.

---

## What you now have

```text
You — Telegram / Claude Desktop / Claude Code / Web
       │
       ▼
OpenClaw Gateway (localhost:4319)
       │   personas: Sally · Mary · Dom · Kristy
       │   skills: this repo's skills/ + your own
       ▼
HealthClaw Guardrails (localhost:5000 + MCP at :3001)
       │   PHI redaction · audit · step-up · tenant isolation
       ▼
FHIR Server (HAPI :8080 or Medplum)
       │
       ▼
Your records — never leave this machine
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `openclaw: command not found` | Install didn't complete or shell PATH not refreshed | Re-run installer; `exec $SHELL` |
| `/health` shows FHIR upstream **down** | HAPI / Medplum container not running | `docker ps`; restart the container |
| `/export` says "HEALTHEX_AUTH_TOKEN not set" | Token never made it to env or Keychain | `security add-generic-password -s healthex -a me -w '<token>'` |
| HealthClaw write returns 401 | Step-up token expired (5-min TTL) | Call `fhir_get_token` immediately before the write |
| HealthClaw write returns 428 | Human-in-the-loop required for clinical write | Add `X-Human-Confirmed: true` after reviewing the proposal |
| MCP tools missing in Claude Desktop | Config file not loaded | Fully quit & relaunch Claude; verify `.mcp.json` JSON validity |
| `curl :8080/fhir/metadata` hangs | HAPI still booting | Wait 30–60s on first start |
| Records ingested but PHI not redacted | `--no-redact` was set OR proxy mode without server | Re-export without `--no-redact`; default is `--redact-mode local` |

---

## Next steps

Once you've got a green Step 5 checklist:

- **Add wearables** — see the `OPEN_WEARABLES_URL` env var and
  [`docs/mac-mini-setup.md`](docs/mac-mini-setup.md) for the Open Wearables
  sidecar (Fitbit / Oura / Whoop / Garmin / Apple Health).
- **Run Curatr** — the `curatr_evaluate` MCP tool finds data quality issues
  (deprecated ICD-9 codes, contradictory smoking history, antibody titers
  flagged as pathology, etc.). See [`skills/curatr/SKILL.md`](../curatr/SKILL.md).
- **Connect Fasten Connect for cross-network coverage** — TEFCA IAS via
  Fasten gives one-auth records from every health system on the QHIN
  network. See [`skills/fasten-connect/SKILL.md`](../fasten-connect/SKILL.md).
- **Daily review workflow** — the `personal-health-records` skill has
  prompts for care-gap analysis, lab-trend review, medication
  reconciliation, and pre-appointment prep.

---

## Where this skill ends and others begin

| If you want to… | Go to |
| --- | --- |
| Understand each MCP tool's contract | [`fhir-r6-guardrails`](../fhir-r6-guardrails/SKILL.md) |
| See exactly what PHI gets redacted | [`phi-redaction`](../phi-redaction/SKILL.md) |
| Wire up an upstream FHIR server (Epic, HAPI, Medplum) | [`fhir-upstream-proxy`](../fhir-upstream-proxy/SKILL.md) |
| Pull records conversationally from HealthEx | [`personal-health-records`](../personal-health-records/SKILL.md) |
| Use the MCP-SDK redacted export | [`healthex-export-redacted`](../healthex-export-redacted/SKILL.md) |
| Copy data tenant-to-tenant inside HealthClaw | [`healthex-export`](../healthex-export/SKILL.md) |
| Find data quality issues and propose fixes | [`curatr`](../curatr/SKILL.md) |
| Connect via TEFCA IAS / Fasten Stitch | [`fasten-connect`](../fasten-connect/SKILL.md) |

If a skill above is missing, ping `@aks129` on the
[HealthClaw repo](https://github.com/aks129/HealthClawGuardrails/issues).
