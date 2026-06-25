# Winters SMBP Demo — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the net-new HealthClaw platform capabilities so the two Winters SMBP demos run on real product, ending on a real clinician SMBP report.

**Architecture:** A new `r6/smbp/` module mirroring `r6/actions/` / `r6/sdc/`. Pure logic (triage, monitoring math, bilingual content, report computation) is Flask-free and unit-tested; a thin Flask blueprint owns auth/audit/step-up/store I/O. Blood-pressure readings are FHIR Observations; the clinician report is computed from them and rendered to HTML + PDF as a FHIR DocumentReference. Outbound patient contact reuses the existing `r6/actions/` propose→confirm→commit loop.

**Tech Stack:** Python 3.11+ / Flask / SQLAlchemy, `reportlab` (PDF), pytest. CI runs Python 3.11 — no backslashes inside f-string `{...}` expressions.

**Spec:** `docs/superpowers/specs/2026-06-18-winters-smbp-demo-design.md`

**Conventions to follow (read before starting):**
- Resource types gated by `R6Resource.SUPPORTED_TYPES` (`r6/models.py`) and `R6_RESOURCE_TYPES` (`r6/validator.py`). `Observation` is already supported; `DocumentReference` is already supported.
- Observation/Document writes: construct `R6Resource(resource_type=, resource_json=json.dumps(...), resource_id=, tenant_id=)`, `db.session.add`, `db.session.commit`. The generated id lives in the column; never embed it in the JSON body (sha256 stays consistent). See `r6/routes.py` create path (~line 546).
- `record_audit_event(event_type, resource_type, resource_id, agent_id=, tenant_id=, detail=)` from `r6.audit`. `detail` is counts/status only — never PHI.
- `validate_step_up_token(token, tenant_id)` → `(bool, str)` — **destructure both**; never coerce the tuple to a boolean.
- Blueprint registration goes in `main.py` after the existing `register_blueprint` calls (~line 142).
- conftest fixtures: `client`, `app`, `tenant_id` (='test-tenant'), `auth_headers` (tenant + step-up), `tenant_headers` (tenant only). Read-auth flag OFF by default in tests.

## File structure

```text
r6/smbp/
  __init__.py    package docstring
  triage.py      [pure] classify(systolic, diastolic, symptoms) -> TriageResult; bands + symptom screen + emergency cutout
  monitoring.py  [pure] averages(observations) + adherence(session, observations); AM/PM derivation; BP Observation builder
  content.py     [pure] bilingual (en/es, <=6th grade) message catalog: msg(key, lang, **fmt)
  report.py      [pure compute + render] build_report(session, observations) -> dict; render_html(report) -> str; render_pdf(report) -> bytes
  models.py      SMBPSession SQLAlchemy model (app state, like ProposedAction)
  routes.py      Flask blueprint: /r6/smbp/enroll, /reading, /report/<id>; auth/audit/step-up/store I/O
tests/
  test_smbp_triage.py
  test_smbp_monitoring.py
  test_smbp_content.py
  test_smbp_report.py
  test_smbp_routes.py
  test_smbp_seed.py
scripts/seed_winters_demo.py   composite Marisol + Mr. Ray + 14 days of readings
```

---

## Task 1: Module skeleton + SMBPSession model

**Files:**
- Create: `r6/smbp/__init__.py`, `r6/smbp/models.py`
- Test: `tests/test_smbp_monitoring.py` (model test lives here for now; pure-logic tests added in Task 3)

- [ ] **Step 1: Write the failing test**

Create `tests/test_smbp_monitoring.py`:

```python
import json

from r6.smbp.models import SMBPSession
from r6.models import db


def test_smbp_session_persists(app):
    with app.app_context():
        s = SMBPSession(tenant_id="t1", patient_ref="Patient/p1",
                        language="es", days=14)
        db.session.add(s)
        db.session.commit()
        got = SMBPSession.query.filter_by(tenant_id="t1").first()
        assert got is not None
        assert got.patient_ref == "Patient/p1"
        assert got.language == "es"
        assert got.days == 14
        assert got.id  # uuid assigned
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_smbp_monitoring.py::test_smbp_session_persists -v`
Expected: FAIL (`ModuleNotFoundError: r6.smbp`).

- [ ] **Step 3: Create the package + model**

Create `r6/smbp/__init__.py`:

```python
"""SMBP (self-measured blood pressure) — monitoring, triage, report, bilingual content."""
```

Create `r6/smbp/models.py`:

```python
"""SMBP session model — app state for a 14-day home BP monitoring order.

Like ProposedAction in r6/actions, this is application state (not a FHIR
resource); the readings themselves are FHIR Observations.
"""

import uuid
from datetime import datetime, timezone

from r6.models import db


def _utcnow():
    return datetime.now(timezone.utc)


class SMBPSession(db.Model):
    __tablename__ = "smbp_sessions"

    id = db.Column(db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    patient_ref = db.Column(db.String(128), nullable=False)
    language = db.Column(db.String(8), nullable=False, default="en")
    days = db.Column(db.Integer, nullable=False, default=14)
    started = db.Column(db.DateTime, nullable=False, default=_utcnow)
    consent_captured = db.Column(db.Boolean, nullable=False, default=False)

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "patient_ref": self.patient_ref,
            "language": self.language,
            "days": self.days,
            "started": self.started.isoformat() if self.started else None,
            "consent_captured": self.consent_captured,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_smbp_monitoring.py::test_smbp_session_persists -v`
Expected: PASS. (The `app` fixture calls `db.create_all()`, which picks up the new model once imported. If the model isn't created, ensure `tests/test_smbp_monitoring.py` imports `SMBPSession` before the fixture runs — the import above does that.)

- [ ] **Step 5: Commit**

```bash
git add r6/smbp/__init__.py r6/smbp/models.py tests/test_smbp_monitoring.py
git commit -m "feat(smbp): module skeleton + SMBPSession model"
```

---

## Task 2: Triage logic (pure, single source of truth)

**Files:**
- Create: `r6/smbp/triage.py`
- Test: `tests/test_smbp_triage.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smbp_triage.py`:

```python
from r6.smbp.triage import classify, SYMPTOMS


def test_normal_below_threshold():
    r = classify(128, 80)
    assert r["band"] == "normal"
    assert r["emergency"] is False
    assert r["action"] == "log_encourage"


def test_elevated_flag_no_alarm():
    r = classify(150, 92)
    assert r["band"] == "elevated"
    assert r["action"] == "log_flag"
    assert r["emergency"] is False


def test_stage_followup_within_week():
    r = classify(168, 104)
    assert r["band"] == "followup"
    assert r["action"] == "symptom_screen_then_visit"
    assert r["emergency"] is False


def test_urgency_recheck_no_symptoms():
    r = classify(184, 118)
    assert r["band"] == "urgent"
    assert r["action"] == "recheck_5min_then_careteam"
    assert r["emergency"] is False


def test_emergency_high_with_symptom():
    r = classify(184, 118, symptoms=["chest_pain"])
    assert r["band"] == "emergency"
    assert r["emergency"] is True
    assert r["action"] == "call_911"


def test_emergency_diastolic_alone_triggers_high_band():
    # >= 180/120 with no symptoms is urgent (recheck), not emergency
    assert classify(150, 122)["band"] == "urgent"


def test_any_symptom_at_high_reading_is_emergency():
    for s in SYMPTOMS:
        assert classify(190, 100, symptoms=[s])["emergency"] is True


def test_symptom_at_normal_reading_not_emergency():
    # symptoms only escalate at >=180/120 per spec table
    assert classify(120, 80, symptoms=["severe_headache"])["emergency"] is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/test_smbp_triage.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

Create `r6/smbp/triage.py`:

```python
"""SMBP triage — the single source of truth for the §2.2 decision logic.

Pure functions. The hypertension-coordinator skill, the Demo 2 voice script,
and the clinician report flags all classify through here so they cannot drift.

Administrative logic only: this classifies a reading into an action band; it
never produces clinical advice text.
"""

# Home BP diagnostic threshold (NOT the office 140/90).
HOME_SYSTOLIC = 135
HOME_DIASTOLIC = 85

# Severe thresholds that gate the emergency pathway.
SEVERE_SYSTOLIC = 180
SEVERE_DIASTOLIC = 120

# The 6-item symptom screen (asked in the patient's language elsewhere).
SYMPTOMS = (
    "chest_pain",
    "trouble_breathing",
    "vision_change",
    "one_sided_weakness",
    "trouble_speaking",
    "severe_headache",
)


def classify(systolic, diastolic, symptoms=None):
    """Classify a reading into a triage band + agent action.

    Returns a dict: {band, action, emergency, rationale, threshold}.
    `symptoms` is an iterable of SYMPTOMS keys the patient endorsed.
    """
    symptoms = [s for s in (symptoms or []) if s in SYMPTOMS]
    severe = systolic >= SEVERE_SYSTOLIC or diastolic >= SEVERE_DIASTOLIC

    if severe and symptoms:
        return _result("emergency", "call_911", True,
                       "Possible hypertensive emergency")
    if severe:
        return _result("urgent", "recheck_5min_then_careteam", False,
                       "Hypertensive urgency")
    if systolic >= 160 or diastolic >= 100:
        return _result("followup", "symptom_screen_then_visit", False,
                       "Needs timely follow-up, not the ED")
    if systolic >= HOME_SYSTOLIC or diastolic >= HOME_DIASTOLIC:
        return _result("elevated", "log_flag", False,
                       "Elevated — the report and the visit handle it")
    return _result("normal", "log_encourage", False,
                   "Within home target range")


def _result(band, action, emergency, rationale):
    return {
        "band": band,
        "action": action,
        "emergency": emergency,
        "rationale": rationale,
        "threshold": {"systolic": HOME_SYSTOLIC, "diastolic": HOME_DIASTOLIC},
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/test_smbp_triage.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add r6/smbp/triage.py tests/test_smbp_triage.py
git commit -m "feat(smbp): triage bands + symptom screen + emergency cutout (pure)"
```

---

## Task 3: Monitoring math + BP Observation builder (pure)

**Files:**
- Create: `r6/smbp/monitoring.py`
- Test: extend `tests/test_smbp_monitoring.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_smbp_monitoring.py`:

```python
from r6.smbp.monitoring import build_bp_observation, slot_of, averages, adherence


def test_build_bp_observation_shape():
    obs = build_bp_observation("Patient/p1", 142, 88, "2026-06-01T08:00:00Z")
    assert obs["resourceType"] == "Observation"
    assert obs["code"]["coding"][0]["code"] == "85354-9"
    comps = {c["code"]["coding"][0]["code"]: c["valueQuantity"]["value"]
             for c in obs["component"]}
    assert comps["8480-6"] == 142  # systolic
    assert comps["8462-4"] == 88   # diastolic
    assert obs["subject"] == {"reference": "Patient/p1"}
    assert obs["effectiveDateTime"] == "2026-06-01T08:00:00Z"


def test_slot_of_am_pm():
    assert slot_of("2026-06-01T08:00:00Z") == "AM"
    assert slot_of("2026-06-01T19:30:00Z") == "PM"


def _obs(s, d, when):
    return build_bp_observation("Patient/p1", s, d, when)


def test_averages_am_pm_overall():
    obs = [_obs(140, 90, "2026-06-01T08:00:00Z"),
           _obs(150, 100, "2026-06-01T20:00:00Z"),
           _obs(130, 80, "2026-06-02T08:00:00Z")]
    a = averages(obs)
    assert a["am"] == {"systolic": 135, "diastolic": 85}   # (140+130)/2, (90+80)/2
    assert a["pm"] == {"systolic": 150, "diastolic": 100}
    assert a["overall"] == {"systolic": 140, "diastolic": 90}  # (140+150+130)/3, (90+100+80)/3
    assert a["valid_days"] == 2


def test_adherence_rate():
    # 14-day session prescribes 2 readings/day = 28; we have 3
    obs = [_obs(140, 90, "2026-06-01T08:00:00Z"),
           _obs(150, 100, "2026-06-01T20:00:00Z"),
           _obs(130, 80, "2026-06-02T08:00:00Z")]
    a = adherence(days=14, observations=obs)
    assert a["completed"] == 3
    assert a["prescribed"] == 28
    assert a["rate"] == round(3 / 28, 2)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/test_smbp_monitoring.py -v`
Expected: FAIL on the new tests (`ImportError`).

- [ ] **Step 3: Implement**

Create `r6/smbp/monitoring.py`:

```python
"""SMBP monitoring math — pure. BP Observation builder + averages + adherence.

Readings are FHIR BP-panel Observations (LOINC 85354-9) with systolic (8480-6)
and diastolic (8462-4) components in mm[Hg]. AM/PM is derived from the
effectiveDateTime hour (< 12:00 local-naive => AM, else PM).
"""

UCUM_MMHG = {"unit": "mm[Hg]", "system": "http://unitsofmeasure.org", "code": "mm[Hg]"}


def build_bp_observation(patient_ref, systolic, diastolic, effective):
    """Return a FHIR BP-panel Observation dict (no id; the store assigns one)."""
    return {
        "resourceType": "Observation",
        "status": "final",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
            "code": "vital-signs"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9",
                             "display": "Blood pressure panel"}]},
        "subject": {"reference": patient_ref},
        "effectiveDateTime": effective,
        "component": [
            {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6",
                                  "display": "Systolic blood pressure"}]},
             "valueQuantity": {"value": systolic, **UCUM_MMHG}},
            {"code": {"coding": [{"system": "http://loinc.org", "code": "8462-4",
                                  "display": "Diastolic blood pressure"}]},
             "valueQuantity": {"value": diastolic, **UCUM_MMHG}},
        ],
    }


def _components(obs):
    """Return (systolic, diastolic) from a BP-panel Observation, or (None, None)."""
    sys_v = dia_v = None
    for c in obs.get("component", []):
        code = c.get("code", {}).get("coding", [{}])[0].get("code")
        val = c.get("valueQuantity", {}).get("value")
        if code == "8480-6":
            sys_v = val
        elif code == "8462-4":
            dia_v = val
    return sys_v, dia_v


def slot_of(effective):
    """AM/PM from an ISO effectiveDateTime (hour < 12 => AM)."""
    # effective like '2026-06-01T08:00:00Z' — take the hour field directly.
    try:
        hour = int(effective[11:13])
    except (ValueError, IndexError):
        return "AM"
    return "AM" if hour < 12 else "PM"


def _avg(pairs):
    if not pairs:
        return None
    sys_vals = [s for s, _ in pairs]
    dia_vals = [d for _, d in pairs]
    return {"systolic": round(sum(sys_vals) / len(sys_vals)),
            "diastolic": round(sum(dia_vals) / len(dia_vals))}


def averages(observations):
    """Compute AM, PM, and overall systolic/diastolic averages + valid_days."""
    am, pm, allp = [], [], []
    days = set()
    for obs in observations:
        s, d = _components(obs)
        if s is None or d is None:
            continue
        eff = obs.get("effectiveDateTime", "")
        allp.append((s, d))
        days.add(eff[:10])
        (am if slot_of(eff) == "AM" else pm).append((s, d))
    return {"am": _avg(am), "pm": _avg(pm), "overall": _avg(allp),
            "valid_days": len(days)}


def adherence(days, observations):
    """Completed readings vs prescribed (2/day over the window)."""
    prescribed = days * 2
    completed = len(observations)
    rate = round(completed / prescribed, 2) if prescribed else 0.0
    return {"completed": completed, "prescribed": prescribed, "rate": rate}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/test_smbp_monitoring.py -v`
Expected: PASS (all tests, incl. the model test from Task 1).

- [ ] **Step 5: Commit**

```bash
git add r6/smbp/monitoring.py tests/test_smbp_monitoring.py
git commit -m "feat(smbp): BP observation builder + averages + adherence (pure)"
```

---

## Task 4: Bilingual patient content catalog (pure)

**Files:**
- Create: `r6/smbp/content.py`
- Test: `tests/test_smbp_content.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smbp_content.py`:

```python
from r6.smbp.content import msg, SYMPTOM_PROMPTS


def test_returns_spanish_and_english():
    assert "presión" in msg("reading_prompt", "es").lower()
    assert "blood pressure" in msg("reading_prompt", "en").lower()


def test_unknown_language_falls_back_to_english():
    assert msg("reading_prompt", "fr") == msg("reading_prompt", "en")


def test_readback_formats_values():
    out = msg("reading_readback", "en", systolic=142, diastolic=88, pulse=76)
    assert "142" in out and "88" in out and "76" in out


def test_symptom_prompts_cover_all_six():
    assert set(SYMPTOM_PROMPTS["en"].keys()) == {
        "chest_pain", "trouble_breathing", "vision_change",
        "one_sided_weakness", "trouble_speaking", "severe_headache"}
    assert set(SYMPTOM_PROMPTS["es"].keys()) == set(SYMPTOM_PROMPTS["en"].keys())


def test_unknown_key_raises():
    import pytest
    with pytest.raises(KeyError):
        msg("nonexistent_key", "en")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/test_smbp_content.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

Create `r6/smbp/content.py`:

```python
"""Bilingual (en/es), <=6th-grade SMBP patient content.

Administrative only — no diagnosis, no medication adjustment. `msg(key, lang)`
returns the string, falling back to English for unknown languages. Strings with
{placeholders} are .format()-ed from kwargs.

Extend by adding keys to CATALOG for both 'en' and 'es'. Keep both languages in
sync and at or below a 6th-grade reading level.
"""

CATALOG = {
    "reading_prompt": {
        "en": "Good morning! Time to check your blood pressure. Sit, rest 5 minutes, then send your numbers.",
        "es": "¡Buenos días! Es hora de medir su presión. Siéntese, descanse 5 minutos, y mande sus números.",
    },
    "reading_readback": {
        "en": "I see {systolic}/{diastolic}, pulse {pulse}. Is that right? Reply 1 = Yes, 2 = No.",
        "es": "Veo {systolic}/{diastolic}, pulso {pulse}. ¿Es correcto? Responda 1 = Sí, 2 = No.",
    },
    "reading_saved": {
        "en": "Saved. You have done {completed} of {prescribed} readings. Great work!",
        "es": "Guardado. Lleva {completed} de {prescribed} mediciones. ¡Va muy bien!",
    },
    "teach_sit": {
        "en": "Sit with your back supported and feet flat on the floor.",
        "es": "Siéntese con la espalda apoyada y los pies planos en el piso.",
    },
    "teach_arm": {
        "en": "Rest your arm on a table so the cuff is at the level of your heart.",
        "es": "Apoye el brazo en una mesa para que el brazalete quede a la altura del corazón.",
    },
    "teach_rest": {
        "en": "Rest quietly for 5 minutes. Do not talk during the reading.",
        "es": "Descanse en silencio por 5 minutos. No hable durante la medición.",
    },
    "med_lisinopril": {
        "en": "Your care plan added lisinopril for blood pressure. Take 1 pill each day. It may cause a dry cough or dizziness when you stand up. Tell your care team — do not stop on your own.",
        "es": "Su plan de cuidado agregó lisinopril para la presión. Tome 1 pastilla cada día. Puede causar tos seca o mareo al pararse. Avise a su equipo de salud — no la deje por su cuenta.",
    },
    "ask_care_team": {
        "en": "That is a good question for your care team. I can help you ask them.",
        "es": "Esa es una buena pregunta para su equipo de salud. Le puedo ayudar a preguntarles.",
    },
    "emergency": {
        "en": "These numbers need a provider right away. Please call 911 or go to the emergency room now.",
        "es": "Estos números necesitan atención médica ahora. Por favor llame al 911 o vaya a emergencias ahora.",
    },
}

SYMPTOM_PROMPTS = {
    "en": {
        "chest_pain": "Do you have chest pain?",
        "trouble_breathing": "Do you have trouble breathing?",
        "vision_change": "Any change in your vision?",
        "one_sided_weakness": "Any weakness or numbness on one side?",
        "trouble_speaking": "Any trouble speaking?",
        "severe_headache": "Do you have a very bad headache?",
    },
    "es": {
        "chest_pain": "¿Tiene dolor en el pecho?",
        "trouble_breathing": "¿Tiene dificultad para respirar?",
        "vision_change": "¿Algún cambio en su vista?",
        "one_sided_weakness": "¿Debilidad o entumecimiento en un lado?",
        "trouble_speaking": "¿Dificultad para hablar?",
        "severe_headache": "¿Tiene un dolor de cabeza muy fuerte?",
    },
}


def msg(key, lang, **fmt):
    """Return the catalog string for key+lang (English fallback), formatted."""
    entry = CATALOG[key]  # KeyError on unknown key — caller bug, fail loud
    text = entry.get(lang, entry["en"])
    return text.format(**fmt) if fmt else text
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/test_smbp_content.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add r6/smbp/content.py tests/test_smbp_content.py
git commit -m "feat(smbp): bilingual (en/es) patient content catalog"
```

---

## Task 5: Clinician SMBP report (compute + HTML + PDF)

**Files:**
- Modify: `pyproject.toml` (move `reportlab` from dev group to runtime `dependencies`)
- Create: `r6/smbp/report.py`
- Test: `tests/test_smbp_report.py`

- [ ] **Step 1: Make reportlab a runtime dependency**

In `pyproject.toml`, add `"reportlab>=4.0",` to the `[project] dependencies` array (it currently only appears under `[dependency-groups] dev`). Leave the dev entry as-is. Then run `uv sync`.
Expected: reportlab resolves as a runtime dep.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_smbp_report.py`:

```python
from r6.smbp.monitoring import build_bp_observation
from r6.smbp.report import build_report, render_html, render_pdf


def _obs(s, d, when):
    return build_bp_observation("Patient/p1", s, d, when)


def _readings():
    return [
        _obs(142, 90, "2026-06-01T08:00:00Z"),
        _obs(150, 96, "2026-06-01T20:00:00Z"),
        _obs(134, 86, "2026-06-02T08:00:00Z"),
        _obs(170, 104, "2026-06-02T20:00:00Z"),  # followup band -> flagged
    ]


def test_build_report_core_numbers():
    rep = build_report(patient_ref="Patient/p1", patient_label="Marisol",
                        days=14, observations=_readings())
    assert rep["overall"]["systolic"] == 149  # (142+150+134+170)/4 = 149 exactly
    assert rep["threshold"] == {"systolic": 135, "diastolic": 85}
    assert rep["adherence"]["completed"] == 4
    assert rep["adherence"]["prescribed"] == 28
    # at least one reading flagged above threshold
    assert any(row["flag"] for row in rep["rows"])
    # the 170/104 reading is in the followup band
    flagged = [r for r in rep["rows"] if r["systolic"] == 170][0]
    assert flagged["band"] == "followup"


def test_render_html_contains_threshold_and_average():
    rep = build_report("Patient/p1", "Marisol", 14, _readings())
    html = render_html(rep)
    assert "135/85" in html
    assert "Marisol" in html
    assert "149/" in html  # overall average shown


def test_render_pdf_returns_pdf_bytes():
    rep = build_report("Patient/p1", "Marisol", 14, _readings())
    pdf = render_pdf(rep)
    assert isinstance(pdf, (bytes, bytearray))
    assert pdf[:4] == b"%PDF"
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run python -m pytest tests/test_smbp_report.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 4: Implement**

Create `r6/smbp/report.py`:

```python
"""Clinician-facing SMBP report — pure compute + HTML/PDF rendering.

build_report() computes the per-reading table, AM/PM + overall averages against
the 135/85 home threshold, and adherence. render_html()/render_pdf() format it.
No DB, no Flask. The route layer persists the rendered report as a
DocumentReference.
"""

import html as _html
import io

from r6.smbp.monitoring import averages, adherence, slot_of, _components
from r6.smbp.triage import classify, HOME_SYSTOLIC, HOME_DIASTOLIC


def build_report(patient_ref, patient_label, days, observations):
    """Return a report dict computed from BP-panel Observations."""
    rows = []
    for obs in sorted(observations, key=lambda o: o.get("effectiveDateTime", "")):
        s, d = _components(obs)
        if s is None or d is None:
            continue
        eff = obs.get("effectiveDateTime", "")
        band = classify(s, d)["band"]
        rows.append({
            "when": eff,
            "slot": slot_of(eff),
            "systolic": s,
            "diastolic": d,
            "band": band,
            "flag": band not in ("normal",),
        })
    avg = averages(observations)
    adh = adherence(days, observations)
    return {
        "patient_ref": patient_ref,
        "patient_label": patient_label,
        "days": days,
        "rows": rows,
        "am": avg["am"],
        "pm": avg["pm"],
        "overall": avg["overall"],
        "valid_days": avg["valid_days"],
        "adherence": adh,
        "threshold": {"systolic": HOME_SYSTOLIC, "diastolic": HOME_DIASTOLIC},
        "flagged_count": sum(1 for r in rows if r["flag"]),
    }


def _avg_str(a):
    return f"{a['systolic']}/{a['diastolic']}" if a else "—"


def render_html(report):
    """One-page clinician HTML report."""
    t = report["threshold"]
    thr = f"{t['systolic']}/{t['diastolic']}"
    rows_html = "".join(
        "<tr class='{cls}'><td>{when}</td><td>{slot}</td>"
        "<td>{sys}/{dia}</td><td>{flag}</td></tr>".format(
            cls="flag" if r["flag"] else "",
            when=_html.escape(r["when"]), slot=r["slot"],
            sys=r["systolic"], dia=r["diastolic"],
            flag=("⚑ " + r["band"]) if r["flag"] else "")
        for r in report["rows"])
    label = _html.escape(report["patient_label"])
    overall = _avg_str(report["overall"])
    am = _avg_str(report["am"])
    pm = _avg_str(report["pm"])
    adh = report["adherence"]
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>SMBP Report — {label}</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#111;max-width:760px;margin:24px auto;padding:0 16px}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#555;font-size:13px;margin-bottom:16px}}
 .cards{{display:flex;gap:12px;margin:16px 0}}
 .card{{flex:1;border:1px solid #ddd;border-radius:8px;padding:12px;text-align:center}}
 .card .n{{font-size:22px;font-weight:700}} .card .l{{font-size:11px;color:#666;text-transform:uppercase}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 th,td{{border-bottom:1px solid #eee;padding:6px 8px;text-align:left}}
 tr.flag td{{background:#fff4f4}} .thr{{color:#555;font-size:12px}}
</style></head><body>
<h1>Home Blood Pressure (SMBP) Report — {label}</h1>
<div class="sub">{report['days']}-day home monitoring · {report['valid_days']} valid days ·
 home threshold {thr} (not office 140/90)</div>
<div class="cards">
 <div class="card"><div class="n">{overall}</div><div class="l">Overall avg vs {thr}</div></div>
 <div class="card"><div class="n">{am}</div><div class="l">AM avg</div></div>
 <div class="card"><div class="n">{pm}</div><div class="l">PM avg</div></div>
 <div class="card"><div class="n">{adh['completed']}/{adh['prescribed']}</div><div class="l">Adherence</div></div>
</div>
<table><thead><tr><th>When</th><th>Slot</th><th>BP</th><th>Flag</th></tr></thead>
<tbody>{rows_html}</tbody></table>
<p class="thr">{report['flagged_count']} reading(s) flagged at or above {thr}.
 Administrative summary — not a diagnosis.</p>
</body></html>"""


def render_pdf(report):
    """Render the report to PDF bytes via reportlab."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle)
    from reportlab.lib.styles import getSampleStyleSheet

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, title="SMBP Report")
    styles = getSampleStyleSheet()
    t = report["threshold"]
    thr = f"{t['systolic']}/{t['diastolic']}"
    elems = [
        Paragraph(f"Home Blood Pressure (SMBP) Report — {report['patient_label']}",
                  styles["Title"]),
        Paragraph(f"{report['days']}-day monitoring · {report['valid_days']} valid days · "
                  f"home threshold {thr}", styles["Normal"]),
        Spacer(1, 0.2 * inch),
        Paragraph(f"Overall average: {_avg_str(report['overall'])} (vs {thr}) · "
                  f"AM {_avg_str(report['am'])} · PM {_avg_str(report['pm'])} · "
                  f"Adherence {report['adherence']['completed']}/"
                  f"{report['adherence']['prescribed']}", styles["Normal"]),
        Spacer(1, 0.2 * inch),
    ]
    data = [["When", "Slot", "BP", "Flag"]]
    for r in report["rows"]:
        data.append([r["when"], r["slot"], f"{r['systolic']}/{r['diastolic']}",
                     r["band"] if r["flag"] else ""])
    table = Table(data, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4e5f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
    ]))
    elems.append(table)
    elems.append(Spacer(1, 0.15 * inch))
    elems.append(Paragraph("Administrative summary — not a diagnosis.", styles["Italic"]))
    doc.build(elems)
    return buf.getvalue()
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run python -m pytest tests/test_smbp_report.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock r6/smbp/report.py tests/test_smbp_report.py
git commit -m "feat(smbp): clinician report compute + HTML + PDF (reportlab runtime dep)"
```

---

## Task 6: Flask blueprint (enroll / reading / report) + registration

**Files:**
- Create: `r6/smbp/routes.py`
- Modify: `main.py` (register the blueprint, ~after line 142)
- Test: `tests/test_smbp_routes.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smbp_routes.py`:

```python
import json

from r6.models import R6Resource, db
from r6.smbp.monitoring import build_bp_observation


def test_enroll_creates_session(client, tenant_headers):
    resp = client.post("/r6/smbp/enroll", headers=tenant_headers,
                       json={"patient_ref": "Patient/p1", "language": "es"})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["language"] == "es"
    assert body["id"]


def test_reading_requires_step_up(client, tenant_headers):
    resp = client.post("/r6/smbp/reading", headers=tenant_headers,
                       json={"patient_ref": "Patient/p1", "systolic": 142,
                             "diastolic": 88, "effective": "2026-06-01T08:00:00Z"})
    assert resp.status_code == 401


def test_reading_logs_observation_and_classifies(client, auth_headers, tenant_id, app):
    resp = client.post("/r6/smbp/reading", headers=auth_headers,
                       json={"patient_ref": "Patient/p1", "systolic": 168,
                             "diastolic": 104, "effective": "2026-06-02T20:00:00Z"})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["triage"]["band"] == "followup"
    # an Observation was written for the tenant
    with app.app_context():
        n = R6Resource.query.filter_by(resource_type="Observation",
                                       tenant_id=tenant_id).count()
        assert n >= 1


def test_emergency_reading_flagged_in_response(client, auth_headers):
    resp = client.post("/r6/smbp/reading", headers=auth_headers,
                       json={"patient_ref": "Patient/p1", "systolic": 190,
                             "diastolic": 122, "effective": "2026-06-03T08:00:00Z",
                             "symptoms": ["chest_pain"]})
    assert resp.status_code == 201
    assert resp.get_json()["triage"]["emergency"] is True


def _seed_session_and_readings(client, app, tenant_id, auth_headers):
    enroll = client.post("/r6/smbp/enroll",
                         headers={**auth_headers},
                         json={"patient_ref": "Patient/p1", "language": "en"})
    session_id = enroll.get_json()["id"]
    with app.app_context():
        for s, d, when in [(142, 90, "2026-06-01T08:00:00Z"),
                           (150, 96, "2026-06-01T20:00:00Z"),
                           (134, 86, "2026-06-02T08:00:00Z")]:
            obs = build_bp_observation("Patient/p1", s, d, when)
            db.session.add(R6Resource(resource_type="Observation",
                                      resource_json=json.dumps(obs),
                                      tenant_id=tenant_id))
        db.session.commit()
    return session_id


def test_report_html_and_pdf(client, app, tenant_id, auth_headers, tenant_headers):
    session_id = _seed_session_and_readings(client, app, tenant_id, auth_headers)
    html = client.get(f"/r6/smbp/report/{session_id}", headers=tenant_headers)
    assert html.status_code == 200
    assert b"135/85" in html.data
    pdf = client.get(f"/r6/smbp/report/{session_id}?format=pdf", headers=tenant_headers)
    assert pdf.status_code == 200
    assert pdf.data[:4] == b"%PDF"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/test_smbp_routes.py -v`
Expected: FAIL (404s — blueprint not registered).

- [ ] **Step 3: Implement the blueprint**

Create `r6/smbp/routes.py`:

```python
"""SMBP Flask blueprint — enroll, log reading, clinician report.

Read-shaped endpoints are tenant-authenticated; the reading write requires a
step-up token (it creates an Observation). All FHIR writes emit an AuditEvent.
Outbound patient contact (reminders, calls) is NOT issued here — it goes through
the r6/actions propose -> human-confirm -> commit loop.
"""

import json
import logging

from flask import Blueprint, request, jsonify, Response

from r6.models import R6Resource, db
from r6.audit import record_audit_event
from r6.stepup import validate_step_up_token
from r6.smbp.models import SMBPSession
from r6.smbp.monitoring import build_bp_observation
from r6.smbp.triage import classify
from r6.smbp.report import build_report, render_html, render_pdf

logger = logging.getLogger(__name__)

smbp_blueprint = Blueprint("smbp", __name__, url_prefix="/r6/smbp")


def _tenant():
    return (request.headers.get("X-Tenant-Id") or "").strip() or None


def _oo(severity, code, diagnostics):
    return {"resourceType": "OperationOutcome",
            "issue": [{"severity": severity, "code": code, "diagnostics": diagnostics}]}


@smbp_blueprint.route("/enroll", methods=["POST"])
def enroll():
    tenant_id = _tenant()
    if not tenant_id:
        return jsonify(_oo("error", "security", "X-Tenant-Id required")), 400
    body = request.get_json(silent=True) or {}
    patient_ref = body.get("patient_ref")
    if not patient_ref:
        return jsonify(_oo("error", "invalid", "patient_ref required")), 400
    session = SMBPSession(
        tenant_id=tenant_id,
        patient_ref=patient_ref,
        language=body.get("language", "en"),
        days=int(body.get("days", 14)),
        consent_captured=bool(body.get("consent_captured", False)),
    )
    db.session.add(session)
    db.session.commit()
    record_audit_event("create", "SMBPSession", session.id,
                       agent_id=request.headers.get("X-Agent-Id"),
                       tenant_id=tenant_id, detail="smbp enroll")
    return jsonify(session.to_dict()), 201


@smbp_blueprint.route("/reading", methods=["POST"])
def reading():
    tenant_id = _tenant()
    if not tenant_id:
        return jsonify(_oo("error", "security", "X-Tenant-Id required")), 400

    step_up = request.headers.get("X-Step-Up-Token")
    if not step_up:
        return jsonify(_oo("error", "security",
                           "reading requires X-Step-Up-Token")), 401
    valid, _err = validate_step_up_token(step_up, tenant_id)
    if not valid:
        return jsonify(_oo("error", "security", "Invalid step-up token")), 401

    body = request.get_json(silent=True) or {}
    try:
        systolic = int(body["systolic"])
        diastolic = int(body["diastolic"])
        patient_ref = body["patient_ref"]
        effective = body["effective"]
    except (KeyError, ValueError, TypeError):
        return jsonify(_oo("error", "invalid",
                           "patient_ref, systolic, diastolic, effective required")), 400

    triage = classify(systolic, diastolic, body.get("symptoms"))
    obs = build_bp_observation(patient_ref, systolic, diastolic, effective)
    row = R6Resource(resource_type="Observation",
                     resource_json=json.dumps(obs), tenant_id=tenant_id)
    db.session.add(row)
    db.session.commit()
    record_audit_event("create", "Observation", row.id,
                       agent_id=request.headers.get("X-Agent-Id"),
                       tenant_id=tenant_id,
                       detail="smbp reading band=%s" % triage["band"])
    return jsonify({"observation_id": row.id, "triage": triage}), 201


@smbp_blueprint.route("/report/<session_id>", methods=["GET"])
def report(session_id):
    tenant_id = _tenant()
    if not tenant_id:
        return jsonify(_oo("error", "security", "X-Tenant-Id required")), 400
    session = SMBPSession.query.filter_by(id=session_id, tenant_id=tenant_id).first()
    if session is None:
        return jsonify(_oo("error", "not-found", "session not found")), 404

    # Gather this patient's BP Observations for the tenant.
    rows = R6Resource.query.filter_by(resource_type="Observation",
                                      tenant_id=tenant_id).all()
    observations = []
    for r in rows:
        obs = r.to_fhir_json()
        if obs.get("subject", {}).get("reference") == session.patient_ref:
            observations.append(obs)

    label = session.patient_ref.split("/")[-1]
    rep = build_report(session.patient_ref, label, session.days, observations)

    record_audit_event("read", "SMBPSession", session.id,
                       agent_id=request.headers.get("X-Agent-Id"),
                       tenant_id=tenant_id,
                       detail="smbp report readings=%d" % len(observations))

    if request.args.get("format") == "pdf":
        pdf = render_pdf(rep)
        # Persist a DocumentReference for the generated report.
        _persist_document_reference(tenant_id, session, len(pdf))
        return Response(pdf, mimetype="application/pdf")
    return Response(render_html(rep), mimetype="text/html")


def _persist_document_reference(tenant_id, session, size):
    doc = {
        "resourceType": "DocumentReference",
        "status": "current",
        "type": {"coding": [{"system": "http://loinc.org", "code": "57075-4",
                             "display": "SMBP report"}]},
        "subject": {"reference": session.patient_ref},
        "content": [{"attachment": {"contentType": "application/pdf",
                                    "title": "SMBP report"}}],
    }
    row = R6Resource(resource_type="DocumentReference",
                     resource_json=json.dumps(doc), tenant_id=tenant_id)
    db.session.add(row)
    db.session.commit()
    record_audit_event("create", "DocumentReference", row.id,
                       tenant_id=tenant_id, detail="smbp report pdf")
```

- [ ] **Step 4: Register the blueprint**

In `main.py`, after the Actions blueprint registration (~line 142), add:

```python
# Register SMBP Blueprint
from r6.smbp.routes import smbp_blueprint
app.register_blueprint(smbp_blueprint)
logger.info("SMBP Blueprint registered at /r6/smbp")
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run python -m pytest tests/test_smbp_routes.py -v`
Expected: PASS (6 tests). If `/reading` returns 401 even with `auth_headers`, confirm `auth_headers` carries `X-Step-Up-Token` for `test-tenant` (it does per conftest).

- [ ] **Step 6: Full suite (regression)**

Run: `uv run python -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add r6/smbp/routes.py main.py tests/test_smbp_routes.py
git commit -m "feat(smbp): enroll/reading/report blueprint with guardrails"
```

---

## Task 7: Seed the winters-demo tenant + integration test

**Files:**
- Create: `scripts/seed_winters_demo.py`
- Test: `tests/test_smbp_seed.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_smbp_seed.py`:

```python
from scripts.seed_winters_demo import build_demo_dataset


def test_demo_dataset_has_both_patients_and_readings():
    data = build_demo_dataset()
    labels = {p["label"] for p in data["patients"]}
    assert labels == {"Marisol", "Mr. Ray"}

    marisol = next(p for p in data["patients"] if p["label"] == "Marisol")
    assert marisol["language"] == "es"
    # ~14 days of readings, 2/day
    assert len(marisol["readings"]) >= 24
    # Marisol's overall average is in the confirmed-hypertension range (~138/88)
    from r6.smbp.monitoring import averages, build_bp_observation
    obs = [build_bp_observation("Patient/marisol", s, d, w)
           for (s, d, w) in marisol["readings"]]
    avg = averages(obs)
    assert 134 <= avg["overall"]["systolic"] <= 145

    ray = next(p for p in data["patients"] if p["label"] == "Mr. Ray")
    assert ray["language"] == "en"
    # Mr. Ray has at least one escalation reading (>=160 systolic)
    assert any(s >= 160 for (s, d, w) in ray["readings"])
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/test_smbp_seed.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement the seed script**

Create `scripts/seed_winters_demo.py`:

```python
"""Seed the synthetic `winters-demo` tenant for the SMBP demos.

Composite patients only — no detail traceable to a real person. Marisol
(smartphone, Spanish) trends to a confirmed-hypertension home average ~138/88;
Mr. Ray (landline, English) includes a 164/98 escalation reading.

Usage (against a running server, with a step-up token):
    python scripts/seed_winters_demo.py --base-url http://localhost:5000 \
        --tenant-id winters-demo --step-up-token <token>
"""

import argparse
import json

TENANT = "winters-demo"


def _two_per_day(start_day, systolics_am, systolics_pm, dia_am, dia_pm):
    """Build (systolic, diastolic, effectiveDateTime) tuples, 2/day."""
    out = []
    for i, (sa, sp, da, dp) in enumerate(
            zip(systolics_am, systolics_pm, dia_am, dia_pm)):
        day = f"2026-06-{start_day + i:02d}"
        out.append((sa, da, f"{day}T08:00:00Z"))
        out.append((sp, dp, f"{day}T20:00:00Z"))
    return out


def build_demo_dataset():
    """Return the composite demo dataset (pure data; no I/O)."""
    # 14 days, AM slightly lower than PM, overall ~138/88.
    marisol_am_s = [136, 138, 134, 140, 137, 135, 139, 136, 138, 134, 137, 139, 135, 138]
    marisol_pm_s = [140, 142, 138, 141, 139, 140, 142, 138, 141, 139, 140, 142, 138, 140]
    marisol_am_d = [86, 88, 85, 89, 87, 85, 88, 86, 87, 85, 88, 89, 86, 87]
    marisol_pm_d = [90, 91, 88, 90, 89, 90, 91, 88, 90, 89, 90, 91, 88, 90]
    marisol_readings = _two_per_day(1, marisol_am_s, marisol_pm_s,
                                    marisol_am_d, marisol_pm_d)

    # Mr. Ray: generally high, with a 164/98 escalation reading on day 2 PM.
    ray = [
        (150, 92, "2026-06-01T08:00:00Z"), (158, 96, "2026-06-01T20:00:00Z"),
        (152, 94, "2026-06-02T08:00:00Z"), (164, 98, "2026-06-02T20:00:00Z"),
        (156, 95, "2026-06-03T08:00:00Z"), (159, 97, "2026-06-03T20:00:00Z"),
    ]

    return {
        "tenant_id": TENANT,
        "patients": [
            {"label": "Marisol", "patient_ref": "Patient/marisol",
             "language": "es", "readings": marisol_readings},
            {"label": "Mr. Ray", "patient_ref": "Patient/mr-ray",
             "language": "en", "readings": ray},
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:5000")
    ap.add_argument("--tenant-id", default=TENANT)
    ap.add_argument("--step-up-token", required=True)
    args = ap.parse_args()

    import requests  # local import — only needed for live seeding
    data = build_demo_dataset()
    headers = {"X-Tenant-Id": args.tenant_id,
               "X-Step-Up-Token": args.step_up_token,
               "Content-Type": "application/json"}
    for p in data["patients"]:
        requests.post(f"{args.base_url}/r6/smbp/enroll",
                      headers={"X-Tenant-Id": args.tenant_id,
                               "Content-Type": "application/json"},
                      data=json.dumps({"patient_ref": p["patient_ref"],
                                       "language": p["language"]}))
        for (s, d, when) in p["readings"]:
            requests.post(f"{args.base_url}/r6/smbp/reading", headers=headers,
                          data=json.dumps({"patient_ref": p["patient_ref"],
                                           "systolic": s, "diastolic": d,
                                           "effective": when}))
    print(f"Seeded {args.tenant_id}: "
          f"{sum(len(p['readings']) for p in data['patients'])} readings")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/test_smbp_seed.py -v`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git add scripts/seed_winters_demo.py tests/test_smbp_seed.py
git commit -m "feat(smbp): seed winters-demo composite patients (Marisol, Mr. Ray)"
```

---

## Task 8: Demo 2 voice script + Demo 1 reminder helper (action-layer wiring)

This wires the two channels onto the existing `r6/actions/` loop. It does NOT
re-implement calling/SMS — it produces the action **payloads** (the Bland voice
script and the bilingual reminder body) that the propose→confirm→commit flow sends.

**Files:**
- Create: `r6/smbp/outreach.py`
- Test: `tests/test_smbp_outreach.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smbp_outreach.py`:

```python
from r6.smbp.outreach import reminder_action, voice_reading_script


def test_reminder_action_is_valid_sms_payload():
    act = reminder_action(patient_ref="Patient/p1", to="+15551234567",
                          lang="es", completed=9, prescribed=28)
    assert act["kind"] == "sms"
    assert isinstance(act["payload"]["body"], str) and act["payload"]["body"]
    assert act["payload"]["to"] == "+15551234567"
    # Spanish reminder
    assert "presión" in act["payload"]["body"].lower()


def test_reminder_action_english():
    act = reminder_action("Patient/p1", "+1", "en", 1, 28)
    assert "blood pressure" in act["payload"]["body"].lower()


def test_voice_script_includes_readback_and_symptom_screen():
    script = voice_reading_script(lang="en")
    joined = " ".join(script["steps"]).lower()
    assert "read" in joined  # read-back step present
    assert script["keypad_fallback"] is True
    # symptom screen lines present for all six symptoms
    assert len(script["symptom_screen"]) == 6
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/test_smbp_outreach.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

Create `r6/smbp/outreach.py`:

```python
"""SMBP outreach payload builders — channel wiring for the action layer.

These produce the payloads consumed by r6/actions (propose -> human-confirm ->
commit). They do not place calls or send SMS themselves. Demo 1 = SMS/Telegram
reminder; Demo 2 = Bland voice script (guided reading, read-back, keypad
fallback, symptom screen). Administrative only — no clinical advice.
"""

from r6.smbp.content import msg, SYMPTOM_PROMPTS


def reminder_action(patient_ref, to, lang, completed, prescribed):
    """Build an `sms` action payload for a bilingual reading reminder."""
    body = msg("reading_prompt", lang)
    return {
        "kind": "sms",
        "payload": {"to": to, "body": body,
                    "meta": {"patient_ref": patient_ref,
                             "completed": completed, "prescribed": prescribed}},
    }


def voice_reading_script(lang="en"):
    """Build the Demo 2 Bland voice script: guided reading + read-back + screen."""
    steps = [
        msg("teach_sit", lang),
        msg("teach_arm", lang),
        msg("teach_rest", lang),
        # read-back placeholder filled by the voice agent with the heard values
        msg("reading_readback", lang, systolic="{systolic}",
            diastolic="{diastolic}", pulse="{pulse}"),
    ]
    return {
        "lang": lang,
        "steps": steps,
        "keypad_fallback": True,
        "symptom_screen": list(SYMPTOM_PROMPTS.get(lang, SYMPTOM_PROMPTS["en"]).values()),
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/test_smbp_outreach.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add r6/smbp/outreach.py tests/test_smbp_outreach.py
git commit -m "feat(smbp): Demo 1 reminder + Demo 2 voice-script payload builders"
```

---

## Task 9: Final verification

- [ ] **Step 1: Full Python suite**

Run: `uv run python -m pytest tests/ -q`
Expected: all green, including all new `test_smbp_*.py`.

- [ ] **Step 2: Manual report smoke (optional, requires Flask on :5000)**

```bash
STEP_UP_SECRET=dev python main.py &
TOKEN=$(curl -s -X POST http://localhost:5000/r6/fhir/internal/step-up-token \
  -H "Content-Type: application/json" -H "X-Tenant-Id: winters-demo" -d '{}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
python scripts/seed_winters_demo.py --tenant-id winters-demo --step-up-token "$TOKEN"
# Then open the report (enroll returns a session id; or query the seeded session)
```
Expected: report HTML shows the overall average against 135/85 with the per-reading table.

- [ ] **Step 3: Compliance re-check**

Read `.claude/compliance/hipaa.md` and confirm: every reading write + report read emits an AuditEvent; reading write requires step-up; audit `detail` carries no PHI (band/counts only); seed data is synthetic/composite.

---

## Notes for the implementer

- **Administrative-only invariant:** none of these modules emit clinical advice or medication adjustments. `content.py` med education is plain-language and always defers to the care team. Keep it that way.
- **Single source of truth:** if a triage threshold needs changing, change it only in `r6/smbp/triage.py`. The report and (eventually) the skill/voice script read from there.
- **Phase 2 (NOT in this plan):** photo-of-cuff OCR via Anthropic vision; the escalation→Option-A staff-confirm booking in the command-center→telehealth conversion. The `outreach.py` payload builders and the `followup`/`urgent` triage bands are the seams those will plug into.
- **Do not** wire `reading` to auto-send any patient message — outbound contact stays behind the action-layer human-confirm loop.
