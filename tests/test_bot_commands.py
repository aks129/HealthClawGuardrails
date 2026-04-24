"""
tests/test_bot_commands.py

Unit tests for scripts/bot_commands.py — the helper each OpenClaw agent
execs to handle HealthClaw slash commands. Validates output formatting
for each FHIR read command using mocked Bundle responses.
"""

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import bot_commands as bc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _Args:
    def __init__(self, **kw):
        self.agent = kw.get("agent", "sally")
        self.tenant = kw.get("tenant", "test-tenant")
        self.arg = kw.get("arg")
        self.resource_type = kw.get("resource_type", self.arg)
        self.path = kw.get("path", self.arg)
        self.code = kw.get("code")
        self.patient = kw.get("patient")
        self.count = kw.get("count")


def _bundle(*resources):
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": r} for r in resources],
    }


@pytest.fixture(autouse=True)
def _step_up(monkeypatch):
    monkeypatch.setenv("STEP_UP_SECRET", "test-secret-for-bot-commands")


def _run_and_capture(fn, args):
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        rc = fn(args)
    return rc, captured.getvalue()


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

class TestConditions:

    def test_empty(self):
        with patch.object(bc, "_fhir_get", return_value=_bundle()):
            rc, out = _run_and_capture(bc.cmd_conditions, _Args())
        assert rc == 0
        assert "No Conditions on file" in out

    def test_groups_active_vs_resolved(self):
        active = {
            "resourceType": "Condition",
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": {"coding": [{"display": "Diabetes mellitus", "system": "http://example/icd", "code": "E11.9"}]},
            "onsetDateTime": "2017-02-13",
        }
        resolved = {
            "resourceType": "Condition",
            "clinicalStatus": {"coding": [{"code": "resolved"}]},
            "code": {"coding": [{"display": "Strep throat", "system": "http://example/icd", "code": "J02.0"}]},
            "onsetDateTime": "2020-01-01",
        }
        with patch.object(bc, "_fhir_get", return_value=_bundle(active, resolved)):
            rc, out = _run_and_capture(bc.cmd_conditions, _Args())
        assert rc == 0
        assert "Active conditions (1):" in out
        assert "Diabetes mellitus" in out
        assert "Resolved/inactive conditions (1):" in out
        assert "Strep throat" in out


# ---------------------------------------------------------------------------
# Labs
# ---------------------------------------------------------------------------

class TestLabs:

    def test_formats_quantity_and_flag(self):
        obs = {
            "resourceType": "Observation",
            "code": {"coding": [{"display": "Glucose", "system": "http://loinc.org", "code": "2339-0"}]},
            "effectiveDateTime": "2026-04-20",
            "valueQuantity": {"value": 180, "unit": "mg/dL"},
            "interpretation": [{"coding": [{"code": "H"}]}],
        }
        with patch.object(bc, "_fhir_get", return_value=_bundle(obs)):
            rc, out = _run_and_capture(bc.cmd_labs, _Args())
        assert rc == 0
        assert "180 mg/dL" in out
        assert "[H]" in out

    def test_empty(self):
        with patch.object(bc, "_fhir_get", return_value=_bundle()):
            rc, out = _run_and_capture(bc.cmd_labs, _Args())
        assert "No lab Observations" in out


# ---------------------------------------------------------------------------
# Vitals (BP multi-component)
# ---------------------------------------------------------------------------

class TestVitals:

    def test_multi_component_bp(self):
        bp = {
            "resourceType": "Observation",
            "code": {"coding": [{"display": "Blood pressure panel"}]},
            "effectiveDateTime": "2026-04-20T09:00:00Z",
            "component": [
                {"code": {"coding": [{"display": "Systolic"}]}, "valueQuantity": {"value": 138, "unit": "mmHg"}},
                {"code": {"coding": [{"display": "Diastolic"}]}, "valueQuantity": {"value": 88, "unit": "mmHg"}},
            ],
        }
        with patch.object(bc, "_fhir_get", return_value=_bundle(bp)):
            rc, out = _run_and_capture(bc.cmd_vitals, _Args())
        assert "Systolic 138mmHg" in out
        assert "Diastolic 88mmHg" in out


# ---------------------------------------------------------------------------
# Meds
# ---------------------------------------------------------------------------

class TestMeds:

    def test_groups_active_vs_inactive(self):
        active = {
            "resourceType": "MedicationRequest",
            "status": "active",
            "intent": "order",
            "medicationCodeableConcept": {"coding": [{"display": "Metformin 500 MG"}]},
        }
        stopped = {
            "resourceType": "MedicationRequest",
            "status": "stopped",
            "intent": "order",
            "medicationCodeableConcept": {"coding": [{"display": "Atorvastatin 20 MG"}]},
        }
        with patch.object(bc, "_fhir_get", return_value=_bundle(active, stopped)):
            rc, out = _run_and_capture(bc.cmd_meds, _Args())
        assert "Active medications (1):" in out
        assert "Metformin" in out
        assert "Inactive medications (1):" in out
        assert "Atorvastatin" in out


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

class TestSummary:

    def test_renders_counts(self):
        def fake_get(rtype, params, tenant):
            totals = {"Patient": 1, "Condition": 3, "Observation": 12}
            return {"total": totals.get(rtype, 0)}
        with patch.object(bc, "_fhir_get", side_effect=fake_get):
            rc, out = _run_and_capture(bc.cmd_summary, _Args())
        assert rc == 0
        assert "patients                 1" in out
        assert "conditions               3" in out
        assert "observations             12" in out

    def test_empty_shows_import_help(self):
        with patch.object(bc, "_fhir_get", return_value={"total": 0}):
            rc, out = _run_and_capture(bc.cmd_summary, _Args())
        assert "No clinical data yet" in out
        assert "/import-help" in out


# ---------------------------------------------------------------------------
# Generic fhir
# ---------------------------------------------------------------------------

class TestFhirGeneric:

    def test_requires_type(self):
        rc = bc.cmd_fhir(_Args(arg=None))
        assert rc == 1

    def test_passes_search_params(self):
        captured = {}
        def fake_get(rtype, params, tenant):
            captured.update({"rtype": rtype, "params": params, "tenant": tenant})
            return _bundle()
        with patch.object(bc, "_fhir_get", side_effect=fake_get):
            bc.cmd_fhir(_Args(arg="Condition", code="E11.9", patient="abc", count=5))
        assert captured["rtype"] == "Condition"
        assert captured["params"]["code"] == "E11.9"
        assert captured["params"]["patient"] == "abc"
        assert captured["params"]["_count"] == 5


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

class TestImport:

    def test_missing_file(self):
        rc = bc.cmd_import(_Args(arg="/no/such/file.json"))
        assert rc == 1

    def test_happy_path(self, tmp_path, monkeypatch):
        bundle_path = tmp_path / "b.json"
        bundle_path.write_text(json.dumps({
            "resourceType": "Bundle", "type": "transaction",
            "entry": [{"resource": {"resourceType": "Patient"}}],
        }))

        posted = {}
        def fake_post(url, json, headers, timeout):
            posted["url"] = url
            posted["headers"] = headers
            m = MagicMock()
            m.status_code = 201
            m.headers = {"content-type": "application/json"}
            m.json.return_value = {"context_id": "ctx-123", "items_ingested": 1}
            return m
        with patch.object(bc.requests, "post", side_effect=fake_post):
            rc, out = _run_and_capture(bc.cmd_import, _Args(arg=str(bundle_path)))
        assert rc == 0
        assert "Imported 1 entries" in out
        assert "ctx-123" in out
        assert posted["headers"]["X-Step-Up-Token"]
        assert posted["headers"]["X-Human-Confirmed"] == "true"


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

class TestHelp:

    def test_mentions_every_category(self):
        rc, out = _run_and_capture(bc.cmd_help, _Args())
        assert rc == 0
        for needle in [
            "/dashboard", "/health", "/tasks",
            "/conditions", "/labs", "/vitals", "/meds",
            "/allergies", "/immunizations", "/summary",
            "/import", "/week", "/conflicts",
        ]:
            assert needle in out, f"missing {needle} in /help"
