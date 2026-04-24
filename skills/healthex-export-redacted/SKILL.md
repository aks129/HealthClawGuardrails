---
name: healthex-export-redacted
description: >
  HealthEx → HealthClaw-redacted export via the official MCP Python SDK
  (mcp>=1.2). Use when: (1) Pulling fresh clinical data from HealthEx as the
  upstream source of truth (not from the local HealthClaw store), (2) Writing a
  PHI-redacted snapshot to disk before any ingest, so the raw MCP response
  never hits the filesystem, (3) Producing a single-file JSON or NDJSON bundle
  for downstream import via `/import`, (4) Running the pipeline headlessly
  from a Telegram bot or cron on the Mac mini. For the older direct-REST pull
  against the local FHIR store, see the `healthex-export` skill.
metadata: {"openclaw":{"requires":{"bins":["python3"]},"install":[{"kind":"uv","packages":["mcp>=1.2","httpx"]}]}}
---

# HealthEx Export (MCP, Redacted)

`scripts/export_healthex_mcp.py` is the current HealthEx pull path. It:

1. Opens a Streamable HTTP MCP session to `https://api.healthex.io/mcp`
2. Optionally calls `update_records` + `check_records_status` to refresh
3. Invokes each clinical tool (`get_health_summary`, `get_conditions`,
   `get_medications`, `get_allergies`, `get_immunizations`, `get_vitals`,
   `get_labs`, `get_procedures`, `get_visits`, `search_clinical_notes`)
4. **Redacts PHI in-process before anything touches disk** — via
   `scripts/healthclaw_redact.py`, which mirrors the HealthClaw guardrail
   proxy's redaction rules
5. Writes the result as JSON (default) or NDJSON (one resource per line)

The raw MCP response is never written. Only the redacted payload goes to disk.

## Quick Start

```bash
# Set the HealthEx token (use macOS Keychain on the Mac mini)
export HEALTHEX_AUTH_TOKEN="$(security find-generic-password -s healthex -w)"

# Default — all tools, local redaction, single JSON file
python scripts/export_healthex_mcp.py \
    --tenant-id ev-personal \
    --output exports/healthex-$(date +%Y-%m-%d).json

# NDJSON (one line per FHIR resource — easier to diff / grep)
python scripts/export_healthex_mcp.py \
    --tenant-id ev-personal \
    --output exports/healthex-$(date +%Y-%m-%d).ndjson

# Only the tools you need
python scripts/export_healthex_mcp.py \
    --tenant-id ev-personal \
    --output exports/labs-only.json \
    --tools get_labs get_conditions

# Proxy mode — redact via a running HealthClaw guardrail server instead
python scripts/export_healthex_mcp.py \
    --tenant-id ev-personal \
    --output exports/snap.json \
    --redact-mode proxy \
    --healthclaw-url https://healthclaw.io

# Synthetic-only escape hatch (keeps PHI in output — NEVER use on real data)
python scripts/export_healthex_mcp.py \
    --tenant-id desktop-demo \
    --output exports/demo-raw.json \
    --no-redact
```

## Redaction Rules (identical to the HealthClaw proxy)

| Field                                          | Rule                                        |
| ---------------------------------------------- | ------------------------------------------- |
| `HumanName.given` / `family` / `text`          | Collapsed to initials (`"E. V."`)           |
| `Address.line` / `city` / `postalCode`         | Dropped (state + country kept)              |
| `Identifier.value` (MRN, member, subscriber)   | SHA-256, optional `HEALTHCLAW_REDACT_SALT`  |
| `birthDate`                                    | Truncated to `YYYY`                         |
| `telecom[].value` (phone / email / fax / sms)  | Replaced with `"***"`                       |
| `Patient.photo`                                | Removed entirely                            |
| `text.div` narrative                           | Emptied                                     |
| `note[]` (Condition / Observation / …)         | Emptied                                     |
| Generic flat-dict PHI keys (ssn, dob, …)       | Wiped at any nesting depth                  |
| `code.coding`, `valueQuantity`, dates          | **Preserved** — clinical signal intact      |

`_meta.redaction_stats` in the output counts every redaction performed.

## Telegram / OpenClaw Integration

Bots call this via the `/export` slash command registered in
`scripts/bot_commands.py` → `cmd_export()`. The bot resolves
`HEALTHEX_AUTH_TOKEN` from (1) environment, (2) macOS Keychain service
`healthex`. Output lands in `~/.healthclaw/exports/healthex-<date>.json`.

Typical end-to-end flow over Telegram:

1. `/export`     — pulls HealthEx, redacts, writes bundle
2. `/import <path printed by /export>` — ingests into local HealthClaw
3. `/conditions` / `/labs` / `/summary` — agent reads from local store

## Smoke Test

`tests/test_healthclaw_redact.py` exercises both the redaction rules and the
end-to-end export flow against a mocked MCP session. Part of the CI suite:

```bash
uv run python -m pytest tests/test_healthclaw_redact.py -v
```

For a one-off CLI check without pytest, `scripts/smoke_test.py` runs the same
assertions and prints a redaction summary.

## Relationship to `healthex-export` (legacy)

The older `healthex-export` skill and `scripts/export_healthex_legacy.py`
pull from the **local HealthClaw FHIR store** via direct REST. Keep for
tenant-to-tenant copies. Use **this skill** when the source of truth is
HealthEx itself and you want the MCP SDK + in-process redaction.
