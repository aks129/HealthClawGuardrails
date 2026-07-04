# Lab Reference-Range Interpreter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Interpret FHIR lab `Observation`s against reference ranges — flag each result low/normal/high/critical using HL7 v3 `ObservationInterpretation`, and return clinician + consumer summaries — behind the existing read-auth + audit guardrails.

**Architecture:** New pure module `r6/labs/` mirroring `r6/quality/`: `interpret.py` (sourced LOINC range table + `interpret_observation`), `report.py` (annotate + summaries), `routes.py` (`$interpret` operation registered on `r6_blueprint`). Plus an `fhir_interpret_labs` MCP read tool that forwards to the Flask operation. Resource `referenceRange` wins; curated table is fallback; unknown/mismatch → indeterminate (never a false normal).

**Tech Stack:** Python 3.11+ (Flask, SQLAlchemy via `R6Resource`), pytest; Node/TypeScript (MCP server, jest).

**Reference:** `docs/superpowers/specs/2026-07-03-lab-reference-range-interpreter-design.md`

---

## File Structure

- Create `r6/labs/__init__.py` — package marker.
- Create `r6/labs/interpret.py` — `LOINC_RANGES`, `REFERENCES`, `interpret_observation()`.
- Create `r6/labs/report.py` — `annotate_observation()`, `build_interpretation_summary()`, `build_consumer_summary()`.
- Create `r6/labs/routes.py` — `register_labs_routes(blueprint, deps)`.
- Modify `r6/routes.py` — register the labs routes next to `register_quality_routes`.
- Create `tests/test_labs_interpret.py`, `tests/test_labs_report.py`, `tests/test_labs_routes.py`.
- Modify `services/agent-orchestrator/src/tools.ts` — add `fhir_interpret_labs` tool def + executor.
- Modify `services/agent-orchestrator/src/tools.test.ts` — expect the new tool.
- Modify `adapters/tools.manifest.json` — add the tool (regen from live server).
- Modify `CLAUDE.md` — add "Lab Interpreter" section.

---

## Task 1: Sourced range table + provenance test

**Files:**
- Create: `r6/labs/__init__.py` (empty)
- Create: `r6/labs/interpret.py`
- Test: `tests/test_labs_interpret.py`

- [ ] **Step 1: Write the failing provenance test**

```python
# tests/test_labs_interpret.py
from r6.labs.interpret import LOINC_RANGES, REFERENCES


def test_every_range_has_a_nonempty_source():
    # Principle 4: no un-sourced range may ship.
    for loinc, entry in LOINC_RANGES.items():
        assert entry.get("source"), f"{loinc} ({entry.get('name')}) missing source"
        assert entry["source"] in REFERENCES, f"{loinc} source not in REFERENCES"


def test_core_analytes_present():
    for loinc in ("2823-3", "2951-2", "2345-7", "4548-4", "718-7", "777-3"):
        assert loinc in LOINC_RANGES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_labs_interpret.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'r6.labs.interpret'`

- [ ] **Step 3: Create the package + sourced table**

Create empty `r6/labs/__init__.py`. Then `r6/labs/interpret.py`:

```python
"""Lab reference-range interpreter — pure engine.

Interprets a FHIR Observation's numeric value against a reference range and
returns an HL7 v3 ObservationInterpretation flag. The performing lab's own
Observation.referenceRange always wins; LOINC_RANGES is the fallback. Unknown
LOINC or unit mismatch yields an indeterminate result — never a false 'normal'.

Standards: LOINC (analytes), UCUM (units), HL7 v3 ObservationInterpretation
(flags). Every LOINC_RANGES entry carries a `source` key present in REFERENCES;
a test enforces it. Values are adult defaults and should be clinician-reviewed
(Dr. Magan) before a live demo.

Decision support, not diagnosis.
"""

# Citable provenance for the ranges below. Keys are referenced by each entry.
REFERENCES = {
    "adult-cc": "Standard adult clinical-chemistry reference ranges "
                "(consensus values; MedlinePlus / common laboratory references).",
    "panic": "Common critical (panic) value conventions "
             "(consensus laboratory critical-value tables).",
    "ata-lipid": "Adult lipid/HbA1c targets (ATP III / ADA-style thresholds).",
}

# unit = expected UCUM unit; low/high optional (one-sided ranges allowed);
# crit_low/crit_high optional panic thresholds; sex overrides optional.
LOINC_RANGES = {
    # --- BMP / CMP ---
    "2951-2":  {"name": "Sodium", "unit": "mmol/L", "low": 135, "high": 145,
                "crit_low": 120, "crit_high": 160, "source": "adult-cc"},
    "2823-3":  {"name": "Potassium", "unit": "mmol/L", "low": 3.5, "high": 5.1,
                "crit_low": 2.5, "crit_high": 6.5, "source": "adult-cc"},
    "2075-0":  {"name": "Chloride", "unit": "mmol/L", "low": 98, "high": 107,
                "source": "adult-cc"},
    "2028-9":  {"name": "Carbon dioxide", "unit": "mmol/L", "low": 22, "high": 29,
                "source": "adult-cc"},
    "3094-0":  {"name": "Urea nitrogen (BUN)", "unit": "mg/dL", "low": 7, "high": 20,
                "source": "adult-cc"},
    "2160-0":  {"name": "Creatinine", "unit": "mg/dL", "low": 0.6, "high": 1.3,
                "sex": {"male": {"low": 0.74, "high": 1.35},
                        "female": {"low": 0.59, "high": 1.04}},
                "source": "adult-cc"},
    "2345-7":  {"name": "Glucose", "unit": "mg/dL", "low": 70, "high": 99,
                "crit_low": 50, "crit_high": 500, "source": "adult-cc"},
    "17861-6": {"name": "Calcium", "unit": "mg/dL", "low": 8.6, "high": 10.3,
                "crit_low": 6.0, "crit_high": 13.0, "source": "adult-cc"},
    "33914-3": {"name": "eGFR", "unit": "mL/min/{1.73_m2}", "low": 60,
                "crit_low": 15, "source": "adult-cc"},
    # --- CBC ---
    "718-7":   {"name": "Hemoglobin", "unit": "g/dL", "low": 12.0, "high": 17.5,
                "crit_low": 7.0, "crit_high": 20.0,
                "sex": {"male": {"low": 13.5, "high": 17.5},
                        "female": {"low": 12.0, "high": 15.5}},
                "source": "adult-cc"},
    "6690-2":  {"name": "White blood cell count", "unit": "10*3/uL",
                "low": 4.5, "high": 11.0, "crit_low": 2.0, "crit_high": 30.0,
                "source": "adult-cc"},
    "777-3":   {"name": "Platelets", "unit": "10*3/uL", "low": 150, "high": 400,
                "crit_low": 20, "crit_high": 1000, "source": "panic"},
    # --- Lipids (target-based: high-side except HDL which is low-side) ---
    "2093-3":  {"name": "Total cholesterol", "unit": "mg/dL", "high": 200,
                "source": "ata-lipid"},
    "13457-7": {"name": "LDL cholesterol", "unit": "mg/dL", "high": 130,
                "source": "ata-lipid"},
    "2085-9":  {"name": "HDL cholesterol", "unit": "mg/dL", "low": 40,
                "sex": {"female": {"low": 50}},
                "source": "ata-lipid"},
    "2571-8":  {"name": "Triglycerides", "unit": "mg/dL", "high": 150,
                "source": "ata-lipid"},
    # --- Diabetes ---
    "4548-4":  {"name": "Hemoglobin A1c", "unit": "%", "high": 5.7,
                "source": "ata-lipid"},
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_labs_interpret.py -q`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add r6/labs/__init__.py r6/labs/interpret.py tests/test_labs_interpret.py
git commit -m "feat(labs): sourced LOINC reference-range table + provenance test"
```

---

## Task 2: `interpret_observation()` flagging engine

**Files:**
- Modify: `r6/labs/interpret.py`
- Test: `tests/test_labs_interpret.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_labs_interpret.py`:

```python
from r6.labs.interpret import interpret_observation


def _obs(loinc, value, unit="mmol/L", ref=None):
    o = {"resourceType": "Observation", "status": "final",
         "code": {"coding": [{"system": "http://loinc.org", "code": loinc}]},
         "valueQuantity": {"value": value, "unit": unit}}
    if ref is not None:
        o["referenceRange"] = ref
    return o


def test_normal_potassium_is_N():
    r = interpret_observation(_obs("2823-3", 4.2))
    assert r["flag"] == "N" and r["critical"] is False
    assert r["range_source"] == "table" and r["analyte"] == "Potassium"


def test_high_potassium_is_H():
    assert interpret_observation(_obs("2823-3", 5.6))["flag"] == "H"


def test_critical_high_potassium_is_HH():
    r = interpret_observation(_obs("2823-3", 7.0))
    assert r["flag"] == "HH" and r["critical"] is True


def test_low_and_critical_low():
    assert interpret_observation(_obs("2823-3", 3.0))["flag"] == "L"
    assert interpret_observation(_obs("2823-3", 2.0))["flag"] == "LL"


def test_resource_range_wins_over_table():
    # Value 5.6 is table-high, but the lab's own range makes it normal.
    ref = [{"low": {"value": 3.0}, "high": {"value": 6.0}}]
    r = interpret_observation(_obs("2823-3", 5.6, ref=ref))
    assert r["flag"] == "N" and r["range_source"] == "resource"


def test_unit_mismatch_is_indeterminate():
    r = interpret_observation(_obs("2823-3", 4.2, unit="mg/dL"))
    assert r["flag"] is None and r["range_source"] == "none"
    assert "indeterminate" in r["note"].lower()


def test_unknown_loinc_is_indeterminate():
    r = interpret_observation(_obs("9999-9", 4.2))
    assert r["flag"] is None and r["range_source"] == "none"


def test_missing_value_is_indeterminate():
    o = {"resourceType": "Observation",
         "code": {"coding": [{"system": "http://loinc.org", "code": "2823-3"}]}}
    assert interpret_observation(o)["flag"] is None


def test_sex_specific_hemoglobin():
    female = {"resourceType": "Patient", "gender": "female"}
    male = {"resourceType": "Patient", "gender": "male"}
    # 12.5 g/dL: normal for female (>=12.0), low for male (<13.5)
    assert interpret_observation(_obs("718-7", 12.5, unit="g/dL"), female)["flag"] == "N"
    assert interpret_observation(_obs("718-7", 12.5, unit="g/dL"), male)["flag"] == "L"


def test_component_only_observation_skipped():
    o = {"resourceType": "Observation",
         "code": {"coding": [{"system": "http://loinc.org", "code": "55284-4"}]},
         "component": [{"code": {"coding": [{"code": "8480-6"}]},
                        "valueQuantity": {"value": 138, "unit": "mmHg"}}]}
    assert interpret_observation(o)["range_source"] == "none"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/test_labs_interpret.py -q`
Expected: FAIL — `ImportError: cannot import name 'interpret_observation'`

- [ ] **Step 3: Implement the engine**

Append to `r6/labs/interpret.py`:

```python
LOINC_SYSTEM = "http://loinc.org"


def _loinc(obs):
    for c in obs.get("code", {}).get("coding", []):
        if c.get("system") == LOINC_SYSTEM and c.get("code"):
            return c["code"]
    return None


def _apply_sex(entry, patient):
    low, high = entry.get("low"), entry.get("high")
    gender = (patient or {}).get("gender")
    override = entry.get("sex", {}).get(gender) if gender else None
    if override:
        low = override.get("low", low)
        high = override.get("high", high)
    return low, high


def _resource_range(obs):
    for rr in obs.get("referenceRange", []):
        low = rr.get("low", {}).get("value")
        high = rr.get("high", {}).get("value")
        if low is not None or high is not None:
            return low, high
    return None


def _flag(value, low, high, crit_low, crit_high):
    if crit_low is not None and value < crit_low:
        return "LL"
    if low is not None and value < low:
        return "L"
    if crit_high is not None and value > crit_high:
        return "HH"
    if high is not None and value > high:
        return "H"
    return "N"


def _indeterminate(analyte, loinc, value, unit, reason):
    return {"analyte": analyte, "loinc": loinc, "value": value, "unit": unit,
            "range_source": "none", "low": None, "high": None,
            "flag": None, "critical": False, "note": f"indeterminate: {reason}"}


def interpret_observation(obs, patient=None):
    """Interpret one Observation. Resource range wins; table is fallback."""
    loinc = _loinc(obs)
    entry = LOINC_RANGES.get(loinc)
    analyte = entry["name"] if entry else None
    vq = obs.get("valueQuantity") or {}
    value, unit = vq.get("value"), vq.get("unit")

    if value is None:
        return _indeterminate(analyte, loinc, value, unit, "no numeric value")
    if loinc is None or entry is None:
        return _indeterminate(analyte, loinc, value, unit, "unknown analyte")

    crit_low, crit_high = entry.get("crit_low"), entry.get("crit_high")

    resource_rng = _resource_range(obs)
    if resource_rng is not None:
        low, high = resource_rng
        source, note = "resource", "used the performing lab's reference range"
    else:
        if unit and unit != entry["unit"]:
            return _indeterminate(analyte, loinc, value, unit,
                                  f"unit {unit!r} != expected {entry['unit']!r}")
        low, high = _apply_sex(entry, patient)
        source = "table"
        note = "adult default range" + (
            "" if (patient or {}).get("gender") or not entry.get("sex")
            else "; sex unknown — used non-specific range")

    flag = _flag(value, low, high, crit_low, crit_high)
    return {"analyte": analyte, "loinc": loinc, "value": value, "unit": unit,
            "range_source": source, "low": low, "high": high,
            "flag": flag, "critical": flag in ("LL", "HH"), "note": note}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/test_labs_interpret.py -q`
Expected: PASS (all engine tests)

- [ ] **Step 5: Commit**

```bash
git add r6/labs/interpret.py tests/test_labs_interpret.py
git commit -m "feat(labs): interpret_observation flagging engine (resource-range-wins)"
```

---

## Task 3: Report builders (annotate + clinician + consumer summaries)

**Files:**
- Create: `r6/labs/report.py`
- Test: `tests/test_labs_report.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_labs_report.py
from r6.labs.interpret import interpret_observation
from r6.labs.report import (
    annotate_observation, build_interpretation_summary, build_consumer_summary,
)

V3 = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"


def _obs(loinc, value, unit):
    return {"resourceType": "Observation", "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": loinc}]},
            "valueQuantity": {"value": value, "unit": unit}}


def test_annotate_adds_interpretation_codeableconcept():
    obs = _obs("2823-3", 7.0, "mmol/L")
    out = annotate_observation(obs, interpret_observation(obs))
    coding = out["interpretation"][0]["coding"][0]
    assert coding["system"] == V3 and coding["code"] == "HH"
    assert obs.get("interpretation") is None  # original untouched (copy)


def test_annotate_stamps_table_range_but_not_resource_range():
    obs = _obs("2823-3", 4.2, "mmol/L")
    out = annotate_observation(obs, interpret_observation(obs))
    assert out["referenceRange"][0]["low"]["value"] == 3.5
    assert "HealthClaw" in out["referenceRange"][0]["text"]


def test_annotate_omits_interpretation_when_indeterminate():
    obs = _obs("9999-9", 1, "mmol/L")
    out = annotate_observation(obs, interpret_observation(obs))
    assert "interpretation" not in out


def test_interpretation_summary_counts():
    results = [interpret_observation(_obs("2823-3", 7.0, "mmol/L")),   # HH critical
               interpret_observation(_obs("2823-3", 4.2, "mmol/L")),   # N
               interpret_observation(_obs("9999-9", 1, "mmol/L"))]     # indeterminate
    s = build_interpretation_summary(results)
    assert s["critical"] == 1 and s["normal"] == 1 and s["indeterminate"] == 1
    assert any(f["flag"] == "HH" for f in s["flagged"])


def test_consumer_summary_is_plain_and_has_next_step():
    results = [interpret_observation(_obs("2823-3", 7.0, "mmol/L"))]
    c = build_consumer_summary(results)
    text = " ".join(line["message"] for line in c["lines"]).lower()
    assert "potassium" in text and "clinician" in text
    for banned in ("diagnos", "prescrib", "treatment"):
        assert banned not in text
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/test_labs_report.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'r6.labs.report'`

- [ ] **Step 3: Implement report builders**

```python
# r6/labs/report.py
"""Report builders for lab interpretation — pure (no Flask/DB).

annotate_observation() returns a COPY of the Observation with an HL7 v3
ObservationInterpretation code (and, for table-sourced ranges, a stamped
referenceRange). build_interpretation_summary() is the clinician view;
build_consumer_summary() is the plain-language, outcomes-oriented consumer view.
Neither summary may be placed in audit detail (PHI).
"""
import copy

V3_INTERPRETATION = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"

_DISPLAY = {"N": "Normal", "L": "Low", "H": "High",
            "LL": "Critically low", "HH": "Critically high"}


def annotate_observation(obs, result):
    out = copy.deepcopy(obs)
    flag = result.get("flag")
    if flag:
        out["interpretation"] = [{"coding": [{
            "system": V3_INTERPRETATION, "code": flag,
            "display": _DISPLAY.get(flag, flag)}]}]
    if result.get("range_source") == "table":
        rng = {"text": "HealthClaw population default (adult); "
                       "not the performing lab's range"}
        if result.get("low") is not None:
            rng["low"] = {"value": result["low"], "unit": result.get("unit")}
        if result.get("high") is not None:
            rng["high"] = {"value": result["high"], "unit": result.get("unit")}
        out.setdefault("referenceRange", []).insert(0, rng)
    return out


def build_interpretation_summary(results):
    buckets = {"normal": 0, "low": 0, "high": 0, "critical": 0, "indeterminate": 0}
    flagged = []
    for r in results:
        flag = r.get("flag")
        if flag is None:
            buckets["indeterminate"] += 1
            continue
        if r.get("critical"):
            buckets["critical"] += 1
        elif flag == "N":
            buckets["normal"] += 1
        elif flag == "L":
            buckets["low"] += 1
        elif flag == "H":
            buckets["high"] += 1
        if flag != "N":
            flagged.append({"analyte": r.get("analyte"), "value": r.get("value"),
                            "unit": r.get("unit"), "flag": flag})
    return {**buckets, "flagged": flagged, "total": len(results)}


def _consumer_line(r):
    analyte, flag = r.get("analyte"), r.get("flag")
    if flag == "N":
        return {"analyte": analyte, "flag": flag,
                "message": f"Your {analyte.lower()} is within the typical range."}
    if r.get("critical"):
        direction = "well above" if flag == "HH" else "well below"
        return {"analyte": analyte, "flag": flag,
                "message": f"Your {analyte.lower()} is {direction} the typical "
                           f"range — contact your clinician promptly to review it."}
    direction = "above" if flag == "H" else "below"
    return {"analyte": analyte, "flag": flag,
            "message": f"Your {analyte.lower()} is {direction} the typical range — "
                       f"worth discussing with your clinician."}


def build_consumer_summary(results):
    lines = [_consumer_line(r) for r in results if r.get("flag")]
    return {"lines": lines,
            "note": "This is general information to help you understand your "
                    "results — not a diagnosis. Your clinician interprets what "
                    "these numbers mean for you."}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/test_labs_report.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add r6/labs/report.py tests/test_labs_report.py
git commit -m "feat(labs): annotate + clinician/consumer summary builders"
```

---

## Task 4: `$interpret` Flask operation

**Files:**
- Create: `r6/labs/routes.py`
- Modify: `r6/routes.py` (register beside `register_quality_routes`, ~line 2924)
- Test: `tests/test_labs_routes.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_labs_routes.py
import json


def _obs(loinc, value, unit):
    return {"resourceType": "Observation", "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": loinc}]},
            "valueQuantity": {"value": value, "unit": unit}}


def _resp_param(body, name):
    for p in body["parameter"]:
        if p["name"] == name:
            return p
    return None


def test_interpret_single_observation(client, tenant_headers):
    r = client.post("/r6/fhir/Observation/$interpret",
                    headers=tenant_headers, json=_obs("2823-3", 7.0, "mmol/L"))
    assert r.status_code == 200
    body = r.get_json()
    assert body["resourceType"] == "Parameters"
    bundle = _resp_param(body, "return")["resource"]
    interp = bundle["entry"][0]["resource"]["interpretation"][0]["coding"][0]
    assert interp["code"] == "HH"
    assert _resp_param(body, "consumerSummary") is not None
    assert _resp_param(body, "disclaimer") is not None


def test_interpret_bundle(client, tenant_headers):
    bundle = {"resourceType": "Bundle", "type": "collection",
              "entry": [{"resource": _obs("2823-3", 4.2, "mmol/L")},
                        {"resource": _obs("2345-7", 520, "mg/dL")}]}
    r = client.post("/r6/fhir/Observation/$interpret",
                    headers=tenant_headers, json=bundle)
    assert r.status_code == 200
    summary = json.loads(_resp_param(r.get_json(), "summary")["valueString"])
    assert summary["total"] == 2 and summary["critical"] == 1


def test_interpret_requires_tenant(client):
    r = client.post("/r6/fhir/Observation/$interpret", json=_obs("2823-3", 4.2, "mmol/L"))
    assert r.status_code == 400


def test_interpret_empty_input_is_ok(client, tenant_headers):
    r = client.post("/r6/fhir/Observation/$interpret",
                    headers=tenant_headers, json={"resourceType": "Bundle", "entry": []})
    assert r.status_code == 200
    assert json.loads(_resp_param(r.get_json(), "summary")["valueString"])["total"] == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/test_labs_routes.py -q`
Expected: FAIL — 404 (route not registered)

- [ ] **Step 3: Implement the route module**

```python
# r6/labs/routes.py
"""FHIR Observation/$interpret — Flask handler.

Registered on r6_blueprint (under /r6/fhir). Read-shaped: tenant-read-
authenticated + AuditEvent (PHI-free detail). Interprets a single Observation,
a Bundle, or the tenant's stored Observations for ?subject=Patient/<id>.
"""
import json
import logging

from flask import request, jsonify

from r6.models import R6Resource
from r6.audit import record_audit_event
from r6.labs.interpret import interpret_observation
from r6.labs.report import (
    annotate_observation, build_interpretation_summary, build_consumer_summary,
)

logger = logging.getLogger(__name__)

_DISCLAIMER = ("Advisory decision support, not a diagnosis. Reference ranges are "
               "adult population defaults and vary by lab, age, sex, and clinical "
               "context. The performing lab's own reference range takes precedence.")


def register_labs_routes(blueprint, deps):
    operation_outcome = deps["operation_outcome"]
    authenticate_tenant_read = deps["authenticate_tenant_read"]

    def _tenant():
        return (request.headers.get("X-Tenant-Id") or "").strip() or None

    def _observations_from_request(tenant_id):
        """Return (observations, patient_by_ref, ignored_count)."""
        body = request.get_json(silent=True) or {}
        subject = request.args.get("subject")
        for p in body.get("parameter", []) if body.get("resourceType") == "Parameters" else []:
            if p.get("name") == "subject":
                subject = p.get("valueReference", {}).get("reference") or subject
        observations, ignored = [], 0
        if subject:
            rows = R6Resource.query.filter_by(
                resource_type="Observation", tenant_id=tenant_id).all()
            for row in rows:
                obs = row.to_fhir_json()
                if obs.get("subject", {}).get("reference") == subject:
                    observations.append(obs)
        elif body.get("resourceType") == "Bundle":
            for e in body.get("entry", []):
                res = e.get("resource", {})
                if res.get("resourceType") == "Observation":
                    observations.append(res)
                else:
                    ignored += 1
        elif body.get("resourceType") == "Observation":
            observations.append(body)
        elif body:
            ignored += 1
        return observations, ignored

    def _patient_for(obs, tenant_id, cache):
        ref = obs.get("subject", {}).get("reference")
        if not ref or not ref.startswith("Patient/"):
            return None
        if ref in cache:
            return cache[ref]
        row = R6Resource.query.filter_by(
            resource_type="Patient", id=ref.split("/", 1)[1],
            tenant_id=tenant_id).first()
        cache[ref] = row.to_fhir_json() if row else None
        return cache[ref]

    @blueprint.route("/Observation/$interpret", methods=["POST"])
    def interpret_labs():
        tenant_id = _tenant()
        if not tenant_id:
            return jsonify(operation_outcome(
                "error", "security", "X-Tenant-Id required")), 400
        auth_err = authenticate_tenant_read(tenant_id)
        if auth_err is not None:
            return auth_err[0], auth_err[1]

        observations, ignored = _observations_from_request(tenant_id)
        cache, results, annotated = {}, [], []
        for obs in observations:
            patient = _patient_for(obs, tenant_id, cache)
            res = interpret_observation(obs, patient)
            results.append(res)
            annotated.append({"resource": annotate_observation(obs, res)})

        summary = build_interpretation_summary(results)
        summary["ignored"] = ignored
        consumer = build_consumer_summary(results)

        record_audit_event(
            "read", resource_type="Observation", resource_id=None,
            agent_id=request.headers.get("X-Agent-Id"), tenant_id=tenant_id,
            detail=(f"labs $interpret; interpreted={summary['total']} "
                    f"flagged={len(summary['flagged'])} critical={summary['critical']}"))

        return jsonify({
            "resourceType": "Parameters",
            "parameter": [
                {"name": "return",
                 "resource": {"resourceType": "Bundle", "type": "collection",
                              "entry": annotated}},
                {"name": "summary", "valueString": json.dumps(summary)},
                {"name": "consumerSummary", "valueString": json.dumps(consumer)},
                {"name": "disclaimer", "valueString": _DISCLAIMER},
            ],
        }), 200
```

- [ ] **Step 4: Register the routes in `r6/routes.py`**

After the `register_quality_routes(...)` block (~line 2930), add:

```python
# --- Lab reference-range interpreter ($interpret) ---
from r6.labs.routes import register_labs_routes  # noqa: E402

register_labs_routes(r6_blueprint, {
    "operation_outcome": _operation_outcome,
    "authenticate_tenant_read": authenticate_tenant_read,
})
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run python -m pytest tests/test_labs_routes.py -q`
Expected: PASS (4 tests)

- [ ] **Step 6: Run the full suite (no regressions)**

Run: `uv run python -m pytest tests/ -q`
Expected: PASS (prior count + new tests)

- [ ] **Step 7: Commit**

```bash
git add r6/labs/routes.py r6/routes.py tests/test_labs_routes.py
git commit -m "feat(labs): Observation/\$interpret operation (read-auth + audit)"
```

---

## Task 5: `fhir_interpret_labs` MCP tool

**Files:**
- Modify: `services/agent-orchestrator/src/tools.ts` (tool def near `fhir_stats` ~line 385; executor case near ~line 806; new private method near `curatrEvaluate` ~line 1457)
- Modify: `services/agent-orchestrator/src/tools.test.ts` (expected tool list ~line 46)

- [ ] **Step 1: Write the failing test**

In `services/agent-orchestrator/src/tools.test.ts`, add `"fhir_interpret_labs"` to the expected read-tool list assertions (the arrays near lines 46 and 72), and add:

```typescript
it("fhir_interpret_labs posts to /Observation/$interpret and returns Parameters", async () => {
  const fetchMock = jest.spyOn(global, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ resourceType: "Parameters", parameter: [] }),
      { status: 200 }));
  const result = await tools.executeTool("fhir_interpret_labs",
    { observation: { resourceType: "Observation" } },
    { "X-Tenant-Id": "t1" });
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining("/Observation/$interpret"),
    expect.objectContaining({ method: "POST" }));
  expect((result as Record<string, unknown>).resourceType).toBe("Parameters");
  fetchMock.mockRestore();
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd services/agent-orchestrator && npm test -- -t "fhir_interpret_labs"`
Expected: FAIL — unknown tool / no matching case.

- [ ] **Step 3: Add tool definition**

In `tools.ts`, after the `fhir_stats` tool object, add:

```typescript
      {
        name: "fhir_interpret_labs",
        description:
          "Interpret lab Observations against reference ranges — flags each value low/normal/high/critical (HL7 v3 ObservationInterpretation) and returns clinician + consumer summaries. Decision support, not diagnosis. Read-tier.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            observation: { type: "object", description: "A single FHIR Observation to interpret" },
            bundle: { type: "object", description: "A FHIR Bundle of Observations to interpret" },
            subject: { type: "string", description: "Patient reference (e.g. 'Patient/pt-1') — interpret the tenant's stored Observations for this subject" },
          },
          required: [],
        },
      },
```

- [ ] **Step 4: Add executor case**

In the `executeTool` switch, near `case "fhir_stats":`, add:

```typescript
      case "fhir_interpret_labs":
        return this.interpretLabs(
          input.observation as Record<string, unknown> | undefined,
          input.bundle as Record<string, unknown> | undefined,
          input.subject as string | undefined,
          fwdHeaders
        );
```

- [ ] **Step 5: Add the private method**

Near `curatrEvaluate`, add:

```typescript
  private async interpretLabs(
    observation: Record<string, unknown> | undefined,
    bundle: Record<string, unknown> | undefined,
    subject: string | undefined,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const url = subject
      ? `${this.baseUrl}/Observation/$interpret?subject=${encodeURIComponent(subject)}`
      : `${this.baseUrl}/Observation/$interpret`;
    const body = subject ? undefined : JSON.stringify(bundle || observation || {});
    const resp = await fetch(url, {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body,
    });
    if (!resp.ok) {
      return { error: `Lab interpret failed with status ${resp.status}` };
    }
    return (await resp.json()) as Record<string, unknown>;
  }
```

- [ ] **Step 6: Run to verify pass + tsc**

Run: `cd services/agent-orchestrator && npx tsc --noEmit && npm test`
Expected: tsc clean; all jest tests pass (including the new one and the updated tool-list assertions).

- [ ] **Step 7: Commit**

```bash
git add services/agent-orchestrator/src/tools.ts services/agent-orchestrator/src/tools.test.ts
git commit -m "feat(mcp): fhir_interpret_labs read tool -> /Observation/\$interpret"
```

---

## Task 6: Manifest + CLAUDE.md docs

**Files:**
- Modify: `adapters/tools.manifest.json`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Regenerate the manifest entry**

Add `fhir_interpret_labs` to `adapters/tools.manifest.json` (same shape as the other read tools — copy the `fhir_stats` entry's `name`/`description`/`inputSchema`/`annotations` structure, using the tool def from Task 5), and bump `tool_count` from 23 to 24. Verify:

Run: `uv run python -c "from adapters.healthclaw_bridge import load_manifest, to_openai_tools, to_gemini_declarations; m=load_manifest(); assert m['tool_count']==len(m['tools']); assert any(t['name']=='fhir_interpret_labs' for t in m['tools']); print('ok', len(to_openai_tools(m)), len(to_gemini_declarations(m)))"`
Expected: `ok 24 24`

- [ ] **Step 2: Add the CLAUDE.md section**

Add after the "## Quality Measures" section:

```markdown
## Lab Interpreter (Observation/$interpret)

`r6/labs/` — a **decision-support** interpreter (NOT a diagnostic device) that
flags lab `Observation` values against reference ranges. Pure engine
(`interpret.py`) + report builders (`report.py`); Flask handler registered via
`register_labs_routes`.

- **`POST /r6/fhir/Observation/$interpret`** — body is one Observation, a Bundle,
  or `?subject=Patient/<id>` (pulls the tenant's stored Observations). Read-shaped:
  tenant-read-authenticated + AuditEvent (PHI-free detail). Returns a `Parameters`
  with the annotated Observations (HL7 v3 `ObservationInterpretation`), a clinician
  `summary`, a plain-language `consumerSummary`, and a `disclaimer`.
- **Resource range wins:** `Observation.referenceRange` (the performing lab)
  always takes precedence over the built-in `LOINC_RANGES` table. Unknown LOINC or
  unit mismatch → *indeterminate*, never a false "normal".
- **Standards:** LOINC / UCUM / HL7 v3 ObservationInterpretation / FHIR R4. Every
  `LOINC_RANGES` entry carries a cited `source` (enforced by test).
- **Honesty / scope:** adult population defaults (clinician-reviewable); panic
  flags are advisory (never auto-act); v1 has no unit conversion, no
  pediatric/pregnancy ranges, no trend analysis.
- **MCP tool:** `fhir_interpret_labs` (read group) forwards to the operation.
```

- [ ] **Step 3: Full suite once more**

Run: `uv run python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add adapters/tools.manifest.json CLAUDE.md
git commit -m "docs(labs): manifest entry + CLAUDE.md Lab Interpreter section"
```

---

## Post-implementation notes

- **Clinician review:** the `LOINC_RANGES` values are adult defaults marked with
  `source`. Before the live physician demo, have Dr. Magan validate the ranges +
  panic thresholds — this is the "credible" principle in action.
- **Deploy:** Python changes auto-deploy on push (Railway Flask + Vercel). The
  MCP-server tool needs the manual staging-dir `railway up` (see CLAUDE.md
  deploy caveat) — the Flask `$interpret` works independently of that deploy.
- **Not done here (tracked follow-ups):** unit conversion, trend/delta analysis,
  pediatric/pregnancy ranges, broader analyte table.
```
