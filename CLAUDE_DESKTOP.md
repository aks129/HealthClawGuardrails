# Claude Desktop Verification

Step-by-step test plan to verify `healthclaw-guardrails` and `smarthealthconnect` work in Claude Desktop before plugin submission. Run through once; every box must tick.

The plugins ship **two surfaces**:
1. **Skills** — markdown prompts that auto-trigger on natural language (Claude Code only; Claude Desktop does not auto-load skills)
2. **MCP servers** — callable tools (works in both Claude Desktop and Claude Code)

This doc verifies the MCP server surface in Claude Desktop. Skill auto-discovery was already verified via `claude plugin install` earlier.

---

## 0. Prerequisites

- Claude Desktop installed and logged in
- Node.js 18+ and Python 3.11+ on PATH
- Both repos cloned locally:
  - `HealthClawGuardrails/` (this repo)
  - `SmartHealthConnect/` (sibling dir)
- Claude Desktop config file location:
  - **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
  - **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
  - **Linux**: `~/.config/Claude/claude_desktop_config.json`

> Port conflict warning: both backends default to `:5000`. Test them one at a time, OR reconfigure SmartHealthConnect to `:5001` via `PORT=5001 npm run dev`.

---

## 1. HealthClawGuardrails — MCP server verification

### 1a. Start the stack

```bash
cd HealthClawGuardrails

# Terminal A: Flask FHIR facade on :5000
uv sync
STEP_UP_SECRET=dev-step-up-secret-change-in-production uv run python main.py

# Terminal B: MCP server on :3001
cd services/agent-orchestrator
npm ci
npm start
```

Sanity check both are up:

```bash
curl -sf http://localhost:5000/r6/fhir/health   # {"status":"healthy",...}
curl -sf http://localhost:3001/health           # {"status":"healthy",...}
```

### 1b. Seed a demo tenant (one-time)

```bash
TOKEN=$(curl -sf -X POST http://localhost:5000/r6/fhir/internal/step-up-token \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: desktop-demo" \
  -d '{}' | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

curl -sf -X POST http://localhost:5000/r6/fhir/internal/seed \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: desktop-demo" \
  -H "X-Step-Up-Token: $TOKEN" \
  -d '{"tenant_id":"desktop-demo"}'
```

Expected: JSON response with `"created": [...]` listing Patient, Condition, Observations, MedicationRequest.

### 1c. Wire Claude Desktop to the MCP server

Open `claude_desktop_config.json` and merge this in:

```json
{
  "mcpServers": {
    "healthclaw-local": {
      "type": "http",
      "url": "http://localhost:3001/mcp",
      "headers": {
        "X-Tenant-ID": "desktop-demo"
      }
    }
  }
}
```

Save the file and **fully quit Claude Desktop** (tray icon → Quit — not just close the window). Relaunch it.

### 1d. Verify tools are visible

In a new Claude Desktop conversation, click the `🔧` tools icon in the composer. You should see `healthclaw-local` listed with all 14 tools:

- Read tools (9): `context_get`, `fhir_read`, `fhir_search`, `fhir_validate`, `fhir_stats`, `fhir_lastn`, `fhir_permission_evaluate`, `fhir_subscription_topics`, `curatr_evaluate`
- Write tools (3): `fhir_propose_write`, `fhir_commit_write`, `curatr_apply_fix`
- Utility tools (2): `fhir_get_token`, `fhir_seed`

### 1e. Execution test prompts

Run each prompt in a fresh conversation. Tick the box when the described behavior is observed.

- [ ] **Read with redaction**
  > Use `fhir_search` to find all Patient resources, then read the first one with `fhir_read`. Show me the family name.

  ✓ Expected: family name is a single letter + period (e.g. `R.`), not the full surname. This proves PHI redaction is active.

- [ ] **Audit trail**
  > After that read, use `fhir_search` to list the most recent 5 AuditEvent resources. Each should show a `read` action on a Patient.

  ✓ Expected: 5 entries, type=read, matching the previous tool calls.

- [ ] **Observation stats**
  > Use `fhir_stats` for LOINC code 2339-0 (glucose).

  ✓ Expected: JSON with count, min, max, mean. If seed ran, count should be ≥ 1.

- [ ] **Cross-tenant isolation**
  > Use `fhir_read` on Patient/some-bogus-id — it should return 404, not leak another tenant's data.

  ✓ Expected: 404 / not-found OperationOutcome.

- [ ] **Step-up gate (write blocked)**
  > Use `fhir_propose_write` to create a new Observation without calling `fhir_get_token` first. It should be rejected.

  ✓ Expected: 401 Unauthorized with message about X-Step-Up-Token.

- [ ] **Step-up + human-in-the-loop (write allowed only with confirmation)**
  > Call `fhir_get_token`, then `fhir_propose_write` on a Condition, then `fhir_commit_write`. The first commit attempt without human confirmation should return 428.

  ✓ Expected: `fhir_commit_write` returns HTTP 428 Precondition Required until `_humanConfirmed: true` is in the args.

- [ ] **Curatr data-quality check**
  > Use `curatr_evaluate` on any Condition resource. It should return issues + quality_score.

  ✓ Expected: JSON with `issues` array and `quality_score` between 0 and 1.

If all 7 pass, HealthClawGuardrails is production-ready on Claude Desktop.

---

## 2. SmartHealthConnect — MCP server verification

> Stop the HealthClawGuardrails Flask server first to free port `:5000`, OR run SmartHealthConnect on a different port (see step 2a).

### 2a. Build + start the stack

```bash
cd SmartHealthConnect

# Install deps (root + mcp-server)
npm install
cd mcp-server && npm install && npm run build && cd ..

# Start the backend API (defaults to :5000; override if HealthClaw is still running)
npm run dev
# OR
PORT=5001 npm run dev
```

Sanity check:

```bash
curl -sf http://localhost:5000/api/health     # or :5001 if you overrode PORT
```

### 2b. Wire Claude Desktop to the stdio MCP server

Add the SmartHealthConnect entry to `claude_desktop_config.json` alongside healthclaw-local. Use the **absolute path** to the built MCP server on your machine:

```json
{
  "mcpServers": {
    "healthclaw-local": { ... existing ... },
    "smarthealthconnect": {
      "command": "node",
      "args": [
        "C:\\Users\\default.LAPTOP-BOBEDDVK\\OneDrive\\Development - GitHub\\SmartHealthConnect\\mcp-server\\dist\\index.js"
      ],
      "env": {
        "SMARTHEALTHCONNECT_API_URL": "http://localhost:5000"
      }
    }
  }
}
```

> If you launched the backend on port 5001, set `SMARTHEALTHCONNECT_API_URL` to `http://localhost:5001` here.

Quit Claude Desktop fully and relaunch.

### 2c. Verify tools are visible

Click the `🔧` tools icon. You should see `smarthealthconnect` listed with these tools (per SmartHealthConnect/CLAUDE.md):

- Health: `get_health_summary`, `get_conditions`, `get_medications`, `get_vitals`, `get_allergies`
- Family: `get_family_members`, `get_family_health_overview`
- Care: `get_care_gaps`, `get_care_plans`, `generate_care_plan`
- Providers: `find_specialists`
- Research: `find_clinical_trials`, `get_research_insights`
- Journal: `get_health_journal`, `add_journal_entry`
- Appointments: `get_appointment_preps`, `generate_appointment_prep`

### 2d. Execution test prompts

Run each in a fresh conversation. Most require demo data — the app starts with an in-memory store, so either (a) use the built-in demo mode via the web UI first, or (b) expect empty results on first call.

- [ ] **Health summary**
  > Use `get_health_summary` and tell me what you see.

  ✓ Expected: structured summary (conditions, medications, vitals) or an empty-state message. No error.

- [ ] **Care gaps (HEDIS)**
  > Use `get_care_gaps` to identify preventive screenings I'm due for.

  ✓ Expected: array of gaps keyed by HEDIS measure, or empty-state.

- [ ] **Clinical trials filter**
  > Use `find_clinical_trials` to find open trials for type 2 diabetes near zip 02101.

  ✓ Expected: list of trials from ClinicalTrials.gov, each with NCT ID and location.

- [ ] **Human-in-the-loop on writes**
  > Use `add_journal_entry` to log "ran 3 miles this morning, felt great".

  ✓ Expected: Claude Desktop prompts for tool approval before calling the write tool (this is Claude Desktop's default tool-call confirmation; per SmartHealthConnect's MCP guardrails, writes also emit human-in-the-loop notices in the response).

If all 4 pass, SmartHealthConnect is production-ready on Claude Desktop.

---

## 3. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Tool icon doesn't show either server | Config syntax error, Claude Desktop cached stale config | Validate JSON with `python3 -m json.tool claude_desktop_config.json`; fully quit Claude from tray (not just ⨯); relaunch |
| `healthclaw-local` shows but tool calls time out | Flask or agent-orchestrator not running | Check `curl http://localhost:3001/health`; restart services |
| Every healthclaw tool returns "tenant mismatch" | `X-Tenant-ID` header not being forwarded | Confirm the `headers` block is on the healthclaw-local entry, not wrapped under `env` |
| `smarthealthconnect` shows but every call returns an error | Backend API not reachable at `SMARTHEALTHCONNECT_API_URL` | Check the port matches what `npm run dev` bound to; backend logs will show the inbound requests |
| Stdio server never starts (Claude Desktop silently drops it) | `node` not on Claude Desktop's PATH, or mcp-server not built | Use the absolute path to `node.exe` (e.g. `"command": "C:\\Program Files\\nodejs\\node.exe"`); re-run `npm run build` in `mcp-server/` |
| Port 5000 conflict | HealthClaw Flask and SmartHealthConnect backend both default to :5000 | Run `PORT=5001 npm run dev` for SmartHealthConnect and update `SMARTHEALTHCONNECT_API_URL` accordingly |

## 4. Sign-off checklist for plugin submission

- [ ] Both servers appear under the 🔧 tools icon after a Claude Desktop restart
- [ ] HealthClawGuardrails: 7/7 test prompts behave as expected
- [ ] SmartHealthConnect: 4/4 test prompts behave as expected
- [ ] No errors in Claude Desktop's Developer Console (Help → Developer → Toggle Developer Tools) during tool calls
- [ ] Each plugin's tools only appear when that MCP server is running (proves no cross-contamination)

If every box is ticked, mark **Claude Desktop** as a supported platform on the submission form and submit.
