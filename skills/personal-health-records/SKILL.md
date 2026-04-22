---
name: personal-health-records
description: >
  Connect your health records from any US health system via HealthEx, pull your
  complete clinical history, analyze it with Claude, and optionally export to a
  personal de-identified FHIR store with automated data quality curation via
  HealthClaw Guardrails. Supports Epic, Cerner, CommonWell, Carequality, and
  most major US EHR networks. Triggers when a user asks to connect health records,
  pull medical history, review lab results, check immunizations, identify care
  gaps, or export data to a personal FHIR store.
version: 1.0.0
author: Eugene Vestel (fhiriq.com)
license: MIT
tools:
  required:
    - HealthEx:get_health_summary
    - HealthEx:get_conditions
    - HealthEx:get_labs
    - HealthEx:get_immunizations
    - HealthEx:get_medications
    - HealthEx:get_allergies
    - HealthEx:get_vitals
    - HealthEx:get_procedures
    - HealthEx:search
    - HealthEx:update_and_check_recent_records
  optional:
    - healthclaw-local:fhir_seed
    - healthclaw-local:fhir_read
    - healthclaw-local:fhir_search
    - healthclaw-local:fhir_get_token
    - healthclaw-local:curatr_evaluate
    - healthclaw-local:curatr_apply_fix
    - healthclaw-local:fhir_commit_write
references:
  healthex: https://healthex.io
  healthclaw: https://github.com/aks129/HealthClawGuardrails
  smart_health_connect: https://github.com/aks129/SmartHealthConnect
  fhir_spec: https://hl7.org/fhir/R4
  us_core: https://hl7.org/fhir/us/core
---

# Personal health records — HealthEx + HealthClaw

Connect your health records from any US health system, pull your complete clinical history, analyze it with Claude, and optionally store it in a personal FHIR data store with data quality curation.

**Standards:** All resources use **FHIR R4** with **US Core v6.1** profiles — the production standard used by US health systems. The `/r6/fhir` URL path in HealthClaw is a legacy route prefix from the project's experimental R6 ballot resource support; the actual clinical data (Conditions, Observations, Immunizations, etc.) is R4 and validated against US Core required fields.

---

## Setup (do this once)

### 1. Connect HealthEx in Claude.ai

1. Go to **claude.ai → Settings → Integrations**
2. Find **HealthEx** and click **Connect**
3. Authorize with your HealthEx account (create one free at healthex.io if needed)
4. In HealthEx, connect your health systems: Epic, Cerner, CommonWell, Carequality, and most major US health networks are supported
5. Return to Claude — the HealthEx tools are now active in this session

To verify: ask Claude *"Check when my health records were last updated"* — it will call `HealthEx:update_and_check_recent_records` and confirm your connection.

### 2. Optional: Connect HealthClaw for local FHIR store

If you want to store, curate, and own a local de-identified copy of your records:

```bash
# Clone the HealthClaw Guardrails repo
git clone https://github.com/aks129/HealthClawGuardrails
cd HealthClawGuardrails

# Start the local stack (Flask guardrail proxy + MCP server + Redis)
cp .env.example .env        # edit with your STEP_UP_SECRET and ANTHROPIC_API_KEY
docker-compose up -d --build

# Confirm healthy
curl http://localhost:5000/r6/fhir/health
```

Add to `.mcp.json` in the repo root so Claude Code picks it up:

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

---

## Zero-setup demo (no install required)

A public Railway instance is pre-seeded with sample clinical data so you can try the full guardrail stack — MCP tools, curatr data quality, PHI redaction, step-up auth — without cloning anything.

### Claude Desktop

Add this to your Claude Desktop MCP config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "healthclaw-demo": {
      "type": "streamable-http",
      "url": "https://healthclaw.up.railway.app/mcp",
      "headers": { "X-Tenant-ID": "desktop-demo" }
    }
  }
}
```

Restart Claude Desktop, then try:

```
Use the healthclaw-demo tools. Get a step-up token, then search for all
Patients in the store. Read the Condition and Observations for the patient.
Run curatr_evaluate on the Condition — it has an ICD-9 code that should be flagged.
```

### Claude Code

Add to `.mcp.json` in any project directory:

```json
{
  "mcpServers": {
    "healthclaw-demo": {
      "type": "http",
      "url": "https://healthclaw.up.railway.app/mcp",
      "headers": { "X-Tenant-ID": "desktop-demo" }
    }
  }
}
```

### What's in the demo tenant

The `desktop-demo` tenant is auto-seeded on first deploy with:

| Resource | Details |
|---|---|
| Patient | Maria Elena Rivera, DOB 1985-03-15, Boston MA |
| Condition | Diabetes mellitus — **ICD-9 code 250.00** (intentionally deprecated, triggers curatr flag) |
| Observation | Glucose 180 mg/dL, HbA1c 8.1%, BP 138/88 mmHg |
| MedicationRequest | Metformin 500 MG Oral Tablet (RxNorm 860975) |

The ICD-9 code is intentional — it demonstrates the curatr `icd9-deprecated` detection pattern. Ask Claude to evaluate it and propose a fix to ICD-10-CM E11.9.

### Re-seed anytime

The seed endpoint is idempotent and additive:

```bash
# HTTP — call the running server
curl -X POST https://healthclaw.up.railway.app/r6/fhir/internal/seed \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "desktop-demo"}'

# Or via MCP tool
fhir_seed(tenant_id="desktop-demo")
```

### Direct API access (no MCP)

The REST API works without MCP too:

```bash
BASE=https://healthclaw.up.railway.app/r6/fhir

# Health check
curl $BASE/health

# Search patients
curl "$BASE/Patient?_count=10" -H "X-Tenant-Id: desktop-demo"

# Read a specific resource
curl "$BASE/Condition/<id>" -H "X-Tenant-Id: desktop-demo"
```

---

## How to pull your health data

### Quick summary (start here)

Ask Claude:

```
Get my health summary
```

Claude calls `HealthEx:get_health_summary` — returns your active conditions, recent labs,
current medications, allergies, immunizations, and clinical visits in one shot.
Use this as a fast overview before pulling specific categories.

---

### Pulling specific categories

Each category tool paginates backwards through time. Claude handles pagination
automatically — you just specify how many years of history you want.

**Standard pulls (ask Claude these directly):**

```
Get all my conditions from the last 10 years
Get all my lab results since 2014
Get my complete immunization history
Get my medications from the last 5 years
Get all my allergies on record
Get my vital signs from the last 3 years
Get my procedures and surgeries
```

**For Claude Code — explicit pagination pattern:**

When pulling a full history, Claude must paginate until no more data is available.
The response includes a `beforeDate` hint when more pages exist. Claude must keep
calling until pagination stops:

```
Pull my complete condition history going back 15 years. Use get_conditions with
years=12, then check the pagination info in the response — if it says more data
is available, call again with the beforeDate from the response and years set to
cover the remaining range. Keep paginating until all data is retrieved.
```

---

### Full export (all categories, all history)

This pulls everything in one session — conditions, labs, immunizations, allergies,
vitals, medications, procedures — with full pagination across all categories.

```
Pull my complete health history across all categories going back 15 years.
For each category, paginate fully until all data is retrieved.
Summarize what was found: count of records per category, date range covered,
any data gaps or missing values you notice.
```

Expected output: a structured summary of everything in your record across
all connected health systems, with counts by category and date range.

---

## Analysis workflows

### Care gaps and preventive screening

```
Based on my health records, identify any preventive care I may be overdue for.
Consider my age, gender, conditions, and immunization history.
Reference USPSTF guidelines for screening recommendations.
```

Claude checks your records against standard preventive care schedules:
- Colorectal cancer screening (colonoscopy, Cologuard) — USPSTF: age 45+
- Annual flu vaccine — last shot date vs current season
- COVID boosters — series completion and recency
- Lipid panel, HbA1c, blood pressure — based on age/risk factors
- Cancer screenings — mammogram, PSA, skin exam based on demographics

---

### Lab trend analysis

```
Pull my lab results for the last 5 years and identify any values that
have been trending in a concerning direction, even if still within
normal range. Flag anything that has been consistently at the edge
of the reference range.
```

```
Compare my most recent labs to my results from 2 years ago.
What has changed? What looks better, what looks worse?
```

---

### Medication review

```
Review my medication history for the last 5 years.
Identify: any medications that were started then stopped (and why if documented),
any dosage changes over time, and any gaps in chronic medication coverage.
```

---

### Condition timeline

```
Build a chronological timeline of my medical conditions from first
documented to present. For each active condition, note how long I've
had it and whether there is documented treatment.
```

---

### Cross-system reconciliation

When you have records from multiple health systems (e.g., AHN + UPMC),
discrepancies are common. Claude can find them:

```
I have records from multiple health systems. Look for any contradictions
or inconsistencies across my records — same test with different results,
conflicting diagnoses, or the same information recorded differently.
Flag anything that needs reconciliation.
```

---

### Pre-appointment preparation

```
I have an appointment with [specialist type] on [date] for [reason].
Pull my relevant health history and prepare a summary I can bring.
Include: relevant conditions, current medications, recent labs, and
any questions I should ask based on gaps in my record.
```

---

### Second opinion research

```
I was diagnosed with [condition]. Pull everything in my record related
to this diagnosis — symptoms, test results, treatments tried — and
summarize the clinical picture. What does my record show about
how this was worked up?
```

---

## Export to personal FHIR store (HealthClaw pipeline)

If you want to store your data locally with de-identification and quality curation:

### Option A — Automated export (one command)

Requires the HealthClaw repo cloned and running (see Setup above):

```bash
# Get your HealthEx auth token from:
# Claude.ai → Settings → Integrations → HealthEx → copy OAuth Bearer token

HEALTHEX_AUTH_TOKEN=<your_token> python scripts/export_healthex_mcp.py \
  --patient-id my-health-id \
  --tenant-id my-health \
  --years 15 \
  --import \
  --step-up-secret $STEP_UP_SECRET
```

This runs the full pipeline:
1. Pulls all HealthEx categories with pagination
2. Maps to US Core R4 FHIR resources
3. Removes PHI (name, address, phone) while preserving clinical data and dates
4. Pre-tags data quality issues for curatr review
5. POSTs the bundle to your local HAPI store through the guardrail proxy
6. Creates a context envelope with SHA-256 provenance hashes

### Option B — Claude-assisted export (no auth token needed)

Ask Claude to build the bundle from the live session pull:

```
Pull all my health data across all categories, fully paginated.
Then build a FHIR R4 transaction bundle with de-identified US Core resources.
Remove my name, address, and phone but keep my date of birth, gender,
and all clinical codes (SNOMED, ICD-10, LOINC, CVX).
Pre-tag any data quality issues you find with curatr meta annotations.
Write the bundle to healthclaw-bundle-[today's date].json.
```

Then import it:

```bash
python scripts/import_healthex.py \
  --bundle-file healthclaw-bundle-<date>.json \
  --tenant-id my-health \
  --step-up-secret $STEP_UP_SECRET
```

### After import — curatr data quality review

In Claude Code with the local stack running:

```
Search my FHIR store for resources tagged with curatr flags.
For each flagged resource, explain the issue in plain language
and propose a fix. Wait for my confirmation before applying any fix.
```

Claude will:
1. Call `curatr_evaluate` on flagged resources
2. Present each issue with plain-language explanation
3. Propose the exact field change needed
4. Apply approved fixes with `curatr_apply_fix`
5. Create a Provenance record for each fix recording your stated intent
6. Generate an immutable AuditEvent trail of all changes

---

## Common data quality issues Claude will find

When pulling from US health systems, these patterns appear frequently:

| Issue | Severity | Example |
|---|---|---|
| Smoking history contradiction | High | AHN says Never, UPMC says Former — same LOINC code, different encounters |
| Antibody titer flagged H | Medium | Hep B S Ab 59.38 mIU/mL flagged High — this is immune protection, not pathology |
| Missing lab result | Low | Test ordered, no result value in imported record — incomplete C-CDA parse |
| ICD-9 code still present | Critical | Condition coded 250.00 (ICD-9) instead of E11.9 (ICD-10) — retired Oct 2015 |
| Active condition, no treatment | Medium | Psoriasis active since 2017, no MedicationRequest or Procedure linked |
| Duplicate immunizations | Low | Same CVX code, same date, from two different health system imports |

Claude detects all of these automatically during export and presents them for your review.

---

## Telehealth and Telegram access (openclaw bot)

If you want to access your health data on mobile via Telegram:

```bash
# Add bot token to .env
TELEGRAM_BOT_TOKEN=<get from @BotFather>

# Start the openclaw service
docker-compose --profile openclaw up -d openclaw
```

Bot commands:
- `/health` — health summary
- `/conditions` — active conditions list
- `/labs` — most recent lab per test
- `/curatr` — list pending data quality issues
- `/curatr fix <issue>` — start a fix with patient attestation
- `/approve` — confirm pending write (step-up authorization)

The Telegram bot reads from and writes to the same local FHIR store as Claude Code.
Every `/approve` creates the same Provenance record as a Claude Code curatr fix.

---

## What HealthEx covers

HealthEx connects to the US health data exchange networks (CommonWell, Carequality, eHealth Exchange) and pulls from any participating EHR system including:

- Epic (most major academic medical centers and health systems)
- Cerner/Oracle Health
- MEDITECH
- athenahealth
- AllScripts
- Most major US payers (via USCDI claims data)

Not yet covered: dental records, mental health records from standalone behavioral health systems, records from health systems that have not joined national exchange networks.

---

## Privacy and data ownership

- HealthEx reads your records read-only via SMART on FHIR — it cannot modify records in your EHR
- The HealthClaw local store runs on your machine — your data never leaves your network unless you configure an external upstream
- PHI fields (name, address, phone, facility MRN) are removed before storage
- All reads and writes are logged in an immutable AuditEvent trail
- You control every curatr fix — nothing is applied without explicit confirmation
- Delete your tenant data at any time: `DELETE /r6/fhir/tenant/my-health`

---

## Troubleshooting

**HealthEx tools not available**
- Go to Claude.ai Settings → Integrations → confirm HealthEx shows Connected
- If it shows an error, click Reconnect and re-authorize

**Records seem incomplete**
- Call `HealthEx:update_and_check_recent_records` — it returns a URL to trigger a fresh sync
- Health systems batch-sync on different schedules; allow 24 hours after connecting

**HealthClaw read tools return 400**
- Confirm local stack is running: `curl http://localhost:5000/r6/fhir/health`
- Confirm `.mcp.json` has `X-Tenant-ID` header set
- Confirm Claude Code is opened from the repo root so `.mcp.json` is loaded

**Import fails with step-up error**
- Token TTL is 5 minutes — run `fhir_get_token` immediately before `import_healthex.py`
- Confirm `STEP_UP_SECRET` in `.env` matches what the Flask server is using

---

## Reference — HealthEx tool quick reference

| Tool | Use when | Key params |
|---|---|---|
| `get_health_summary` | First call, overview | none |
| `get_conditions` | Diagnoses, medical history | `years`, `beforeDate` |
| `get_labs` | Test results, bloodwork | `years`, `beforeDate` |
| `get_immunizations` | Vaccination history | `years`, `beforeDate` |
| `get_medications` | Prescriptions, dosages | `years`, `beforeDate` |
| `get_allergies` | Drug/food/env allergies | `years`, `beforeDate` |
| `get_vitals` | BP, weight, BMI, HR | `years`, `beforeDate`, `vitals[]` |
| `get_procedures` | Surgeries, imaging, scopes | `years`, `beforeDate` |
| `search` | Cross-category keyword lookup | `query`, `limit` |
| `update_and_check_recent_records` | Check sync status | none |

All paginated tools return a `beforeDate` hint in their response when more data exists.
Always paginate to completion before concluding a category is fully retrieved.
