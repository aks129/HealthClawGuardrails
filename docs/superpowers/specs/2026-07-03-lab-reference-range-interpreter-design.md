# Lab Reference-Range Interpreter — Design

**Date:** 2026-07-03
**Status:** Approved (design), spec under review
**Author:** HealthClaw (Gene Vestel) + Claude

## Purpose

Give agents a guardrailed way to interpret FHIR lab `Observation`s against
reference ranges — flagging each result low / normal / high / critical and
producing a clinician- and patient-readable summary. Clinical usefulness for the
Dr. Gigi Magan physician-group demos, and a table-stakes capability for a lab-
aware health agent. **Decision support, not diagnosis** — the honesty posture
matches the NQF 0018 quality module (calculator, not a certified device).

## Non-goals (v1)

- Not a diagnostic device; no treatment recommendations.
- No terminology-server round-trips (unlike Curatr) — the analyte table is local.
- No trend/delta analysis across time (single most-recent value per analyte).
- No pediatric or pregnancy-specific ranges (adult defaults only; noted in output).
- No unit conversion (e.g. mg/dL ↔ mmol/L) — a unit mismatch yields *indeterminate*,
  never a coerced value. Conversion is a documented follow-up.

## Architecture

New module `r6/labs/`, mirroring `r6/quality/` (pure engine + report builders +
Flask routes, each independently testable). No changes to existing modules
except route registration in `r6/routes.py`.

```
r6/labs/
  __init__.py
  interpret.py   # pure engine: LOINC_RANGES table + interpret_observation()
  report.py      # annotate_observation() + build_interpretation_summary()
  routes.py      # register_labs_routes(blueprint, deps) — auth + audit + I/O
```

### 1. `interpret.py` (pure — no Flask, no DB)

**`LOINC_RANGES`** — curated dict keyed by LOINC code. Chronic-care core:

| Panel | Analytes (LOINC) |
| --- | --- |
| BMP/CMP | Na 2951-2, K 2823-3, Cl 2075-0, CO₂ 2028-9, BUN 3094-0, creatinine 2160-0, glucose 2345-7, Ca 17861-6, eGFR 33914-3 |
| CBC | Hgb 718-7, WBC 6690-2, platelets 777-3 |
| Lipids | total chol 2093-3, LDL 13457-7, HDL 2085-9, triglycerides 2571-8 |
| Diabetes | HbA1c 4548-4 |

Each entry:

```python
{
  "name": "Potassium",
  "unit": "mmol/L",              # expected UCUM unit
  "low": 3.5, "high": 5.1,
  "crit_low": 2.5, "crit_high": 6.5,   # optional panic thresholds
  "sex": {                              # optional sex-specific overrides
    "male":   {"low": ..., "high": ...},
    "female": {"low": ..., "high": ...},
  },
}
```

Sex-specific analytes in v1: Hgb, creatinine, HDL. Ranges sourced from widely
used adult clinical-chemistry references; each entry carries a `source` note in
the module for traceability.

**`interpret_observation(obs, patient=None) -> dict`**

Returns:

```python
{
  "analyte": "Potassium",            # or None if unknown LOINC
  "loinc": "2823-3",
  "value": 6.8, "unit": "mmol/L",
  "range_source": "resource" | "table" | "none",
  "low": 3.5, "high": 5.1,
  "flag": "N" | "L" | "H" | "LL" | "HH" | None,   # HL7 v3 ObservationInterpretation
  "critical": True,
  "note": "adult default range; sex unknown — used non-specific range",
}
```

Logic:

1. Extract the primary LOINC code + `valueQuantity` (value + unit). Component-only
   observations (e.g. BP panels) are **skipped** with `range_source:"none"` — BP
   is the quality module's domain.
2. **Resource `referenceRange` wins.** If `obs.referenceRange[0]` has `low`/`high`,
   use it (`range_source:"resource"`). Critical flags only when the table also has
   `crit_*` for that analyte.
3. Else fall back to `LOINC_RANGES` (`range_source:"table"`), applying the
   sex-specific override when `patient.gender` is known; when unknown, use the
   non-specific range and record the assumption in `note`.
4. **Unit mismatch** (value unit present and ≠ the range's expected unit) or
   **unknown LOINC** → `range_source:"none"`, `flag:None`, `note:"indeterminate: <reason>"`.
   Never emit a false `N`.
5. Flagging: `< crit_low → LL`, `< low → L`, `> crit_high → HH`, `> high → H`,
   else `N`. `critical = flag in {"LL","HH"}`.

Critical/panic set (has `crit_*`): K⁺, Na⁺, glucose, creatinine, Hgb, platelets.
Panic values are **advisory** — the engine flags, it never acts.

### 2. `report.py` (pure)

- **`annotate_observation(obs, result) -> dict`** — returns a **copy** of the
  Observation with:
  - `interpretation`: a `CodeableConcept` using system
    `http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation`, the
    `flag` code, and a human display. Omitted when `flag is None`.
  - `referenceRange`: when `range_source == "table"`, a stamped range with
    `text` = "HealthClaw population default (adult); not the performing lab's range".
    Never overwrites a resource-provided range.
- **`build_interpretation_summary(results) -> dict`** — counts
  `{normal, low, high, critical, indeterminate}` + a `flagged` list of
  `{analyte, value, unit, flag}`. Clinical data → returned only to the
  authenticated reader; **never** placed in audit detail.

### 3. `routes.py` — `register_labs_routes(blueprint, deps)`

Registered in `r6/routes.py` beside `register_quality_routes` /
`register_sdc_routes`.

**`POST /r6/fhir/Observation/$interpret`**

Input, one of:
- a single `Observation` resource,
- a `Bundle` of Observations,
- `?subject=Patient/<id>` (or a `subject` Parameters value) → pull the tenant's
  stored Observations for that patient.

Read-shaped, exactly like the quality + SDC read operations:
- `authenticate_tenant_read(tenant_id)` (tenant-bound token / SMART bearer when
  `READ_AUTH_ENABLED`, public tenants exempt).
- `record_audit_event("read", "Observation", ...)` — **PHI-free** detail:
  counts only (`interpreted=<n> flagged=<m> critical=<k>`).

Output — a `Parameters` (consistent with `$populate`):

```json
{
  "resourceType": "Parameters",
  "parameter": [
    {"name": "return", "resource": { "Bundle of annotated Observations" }},
    {"name": "summary", "valueString": "<json summary>"},
    {"name": "disclaimer", "valueString":
      "Advisory decision support, not a diagnosis. Reference ranges are adult
       population defaults and vary by lab, age, sex, and clinical context.
       The performing lab's own reference range takes precedence."}
  ]
}
```

### 4. MCP tool `fhir_interpret_labs` (Read group)

In `services/agent-orchestrator`. Forwards to `$interpret`, carrying
`X-Tenant-Id` (+ step-up token for non-public tenants, same as the other read
tools). Added to the manifest so the `adapters/` bridge exposes it to
OpenAI/Gemini. Node jest test asserts the tool relays and returns the Parameters.

## Data flow

```
Agent (any framework)
  → MCP fhir_interpret_labs
    → Flask POST /Observation/$interpret   (tenant-auth, AuditEvent[PHI-free])
      → r6.labs.interpret.interpret_observation()  (resource range → table → flag)
      → r6.labs.report.annotate_observation() + build_interpretation_summary()
    ← Parameters{ annotated Bundle, summary, disclaimer }
```

## Error handling

- Missing/`None` value, non-quantity result → `range_source:"none"`, no flag.
- Unknown LOINC / unit mismatch → indeterminate (never a false normal).
- Empty input / no Observations for subject → 200 with empty Bundle + zero summary.
- Non-Observation resource in input → skipped, counted in an `ignored` tally.

## Honesty posture (explicit, matches quality module)

- The `$interpret` response and the MCP tool description both state: decision
  support, not a certified diagnostic device.
- Ranges are population adult defaults; the performing lab's range always wins.
- Panic flags are advisory ("verify and act per your protocol") — no auto-action.
- `CLAUDE.md` gets a "Lab Interpreter" section stating the same, plus the v1
  scope limits (no unit conversion, no pediatric/pregnancy ranges, no trends).

## Testing

- **Engine** (`test_labs_interpret.py`): each analyte below/normal/above/critical;
  sex-specific (male vs female Hgb); resource-range-wins over table; unit mismatch
  → indeterminate; unknown LOINC → indeterminate; missing value; component-only
  (BP) skipped.
- **Report** (`test_labs_report.py`): interpretation `CodeableConcept` shape +
  system/code; table-sourced `referenceRange` stamped, resource range untouched;
  summary counts; summary never surfaces in audit-safe shape.
- **Routes** (`test_labs_routes.py`): single Observation, Bundle, `?subject=`
  pull; read-auth 401 for non-public header-only; AuditEvent emitted with
  PHI-free detail; disclaimer present; empty-input 200.
- **MCP** (jest): `fhir_interpret_labs` relays and returns the Parameters.

## Follow-ups (tracked, out of v1 scope)

- Unit conversion (mg/dL ↔ mmol/L) for common analytes.
- Trend/delta interpretation across successive results.
- Pediatric / pregnancy reference ranges.
- Broader analyte table (TSH, liver panel, vitamin D).
