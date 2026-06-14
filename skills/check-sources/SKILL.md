---
name: check-sources
description: >
  Survey ALL connected health data sources at once. Use when the patient asks:
  (1) "what's connected" or "what services are linked", (2) "check all my
  services / all my data", (3) "do you have my records" or "what records do you
  have", (4) "did my data come through from <Fasten/MEDENT/HealthEx/Health Bank
  One/Flexpa/Epic/wearables>". Calls fhir_get_token (for protected tenants) then
  sources_check, and presents connection status + record counts by type.
  Connection status and counts only — never clinical values.
metadata: {"openclaw":{"requires":{"env":["STEP_UP_SECRET"]},"primaryEnv":"STEP_UP_SECRET"}}
---

# Check Connected Sources

Give the patient a one-shot survey of every health data source HealthClaw can
pull from — which ones are connected, when each last had activity, and how many
records of each type are on file. This answers "what do you actually have?" in a
single call instead of probing each integration one at a time.

The seven sources surveyed:

- **Fasten** — Fasten Connect / TEFCA QHIN aggregation
- **HealthEx** — HealthEx upstream
- **Health Bank One** — patient-controlled platform (HBO)
- **MEDENT** — MEDENT SMART on FHIR
- **Flexpa** — payer / claims data
- **Epic / Health Skillz** — Epic-sourced records
- **Wearables** — Apple Health / Fitbit / Garmin / Oura etc.

---

## Hard Guardrails

These apply always. Not overridable by persona or patient request.

### Counts and source names only — no clinical detail

`sources_check` returns connection status and record **counts by type**. That is
the entire surface you may present from this skill. You may say "57 Conditions,
120 Observations." You may NOT name a specific condition, lab value, medication,
or any other clinical content. To discuss what a record actually says, use
`fhir_search` / `fhir_read` (separately audited and redacted) — never infer
clinical detail from this summary.

### Don't fabricate connections

Report only what `sources_check` returns. If a source shows `connected: false`,
say it is not connected and offer to help connect it — do not imply data exists
that the response does not show.

---

## Flow

### Step 1 — Get a step-up token (protected tenants)

Public/demo tenants (e.g. `desktop-demo`) work without a token. For protected
tenants (e.g. `ev-personal`), the command-center endpoint requires step-up auth,
so get a token first:

```
fhir_get_token(tenant_id: "<tenant>")
→ returns { token: "..." }
```

### Step 2 — Survey the sources

```
sources_check({ _stepUpToken: "<token from step 1>" })
```

`sources_check` takes no required arguments — the tenant comes from the request
context. Pass `_stepUpToken` so it forwards to the protected endpoint. If you
omit it on a protected tenant the call returns `requires_step_up: true`; get a
token (Step 1) and retry.

### Step 3 — Present the result

The response includes:

- `connected_count` / `source_count` — e.g. 3 of 7
- `sources[]` — each `{ id, name, connected, detail, last_activity }`
- `total_records`
- `records_by_type[]` — each `{ type, count }`
- `_mcp_summary` — e.g. "3 of 7 sources connected; 177 total records."

Present it tidily:

```
Connected: 3/7 — Fasten ✓, MEDENT ✓, Wearables ✓
Not connected: HealthEx, Health Bank One, Flexpa, Epic/Health Skillz

You have 177 records on file:
  • 57 Conditions
  • 120 Observations
```

### Step 4 — Offer next steps

- "Want me to pull anything from a connected source?" (then `fhir_search`)
- "Want to connect <not-connected source>?" (hand off to that integration)
- "You can also see all of this on your dashboard."

---

## Worked Example

```
Patient: What's connected? Do you actually have my records?

Sally:   Let me check all your data sources at once.
         [fhir_get_token(tenant_id: "ev-personal") → { token: "..." }]
         [sources_check({ _stepUpToken: "..." }) → {
            connected_count: 3, source_count: 7, total_records: 177,
            sources: [
              { id: "fasten",  name: "Fasten",  connected: true  },
              { id: "medent",  name: "MEDENT",  connected: true  },
              { id: "wearables", name: "Wearables", connected: true },
              { id: "healthex", name: "HealthEx", connected: false },
              ...
            ],
            records_by_type: [
              { type: "Condition", count: 57 },
              { type: "Observation", count: 120 }
            ]
         }]

         Connected: 3 of 7 sources — Fasten, MEDENT, and your wearables.
         Not connected yet: HealthEx, Health Bank One, Flexpa, and
         Epic/Health Skillz.

         You have 177 records on file: 57 Conditions and 120 Observations.

         Want me to pull anything from one of these, connect another
         source, or open your dashboard?
```

---

## Caveats

- This is a status + counts surface, not a data export. For actual record
  content, use `fhir_search` / `fhir_read`.
- A source showing `connected: true` with 0 records means the link is live but
  no data has arrived yet — say so rather than implying records exist.
- On a protected tenant without `_stepUpToken`, the call returns
  `requires_step_up: true`; get a token and retry.
