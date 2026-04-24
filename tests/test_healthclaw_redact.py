"""
tests/test_healthclaw_redact.py

pytest version of scripts/smoke_test.py — exercises the redaction module
and the end-to-end export pipeline via mocked MCP session. Same assertions,
runs as part of the CI suite.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from healthclaw_redact import redact, RedactionStats  # noqa: E402


# ---------------------------------------------------------------------------
# Canned synthetic payload (no real PHI)
# ---------------------------------------------------------------------------

CANNED_PATIENT = {
    "resourceType": "Patient",
    "id": "pat-001",
    "name": [{"given": ["Eugene"], "family": "Vestel", "text": "Eugene Vestel"}],
    "birthDate": "1981-05-29",
    "gender": "male",
    "address": [{
        "line": ["123 Main St"], "city": "Pittsburgh", "state": "PA",
        "postalCode": "15228", "country": "US",
    }],
    "telecom": [
        {"system": "phone", "value": "412-555-0199"},
        {"system": "email", "value": "eugene@example.com"},
    ],
    "identifier": [{"system": "http://hospitals.example/mrn", "value": "MRN-77234-A"}],
    "text": {"status": "generated", "div": "<div>Eugene Vestel, 44yo male</div>"},
}

CANNED_CONDITION = {
    "resourceType": "Condition",
    "id": "cond-001",
    "subject": {"reference": "Patient/pat-001"},
    "code": {"coding": [{"system": "http://snomed.info/sct", "code": "44054006",
                         "display": "Diabetes mellitus type 2"}]},
    "onsetDateTime": "2019-03-14",
    "note": [{"text": "Patient Eugene Vestel reports worsening symptoms."}],
}

CANNED_LABS_BUNDLE = {
    "resourceType": "Bundle", "type": "searchset",
    "entry": [
        {"resource": {
            "resourceType": "Observation", "id": "obs-001",
            "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4",
                                 "display": "Hemoglobin A1c"}]},
            "valueQuantity": {"value": 6.4, "unit": "%"},
            "effectiveDateTime": "2026-01-15",
        }},
        {"resource": {
            "resourceType": "Observation", "id": "obs-002",
            "code": {"coding": [{"system": "http://loinc.org", "code": "2085-9",
                                 "display": "HDL Cholesterol"}]},
            "valueQuantity": {"value": 52, "unit": "mg/dL"},
            "effectiveDateTime": "2026-01-15",
        }},
    ],
}

CANNED_MEDS_FLAT = [{
    "patientName": "Eugene Vestel", "ssn": "123-45-6789",
    "medication": "Metformin 500mg", "rxnorm": "860975", "startDate": "2019-03-15",
}]


# ---------------------------------------------------------------------------
# Unit: PHI redaction rules
# ---------------------------------------------------------------------------

class TestPatientRedaction:

    def test_name_truncated_to_initials(self):
        redacted, _ = redact(CANNED_PATIENT)
        assert redacted["name"][0]["text"] == "E. V."
        assert "given" not in redacted["name"][0]
        assert "family" not in redacted["name"][0]

    def test_birthdate_coarsened_to_year(self):
        redacted, stats = redact(CANNED_PATIENT)
        assert redacted["birthDate"] == "1981"
        assert stats.birthdates_coarsened == 1

    def test_address_strips_line_city_zip(self):
        redacted, _ = redact(CANNED_PATIENT)
        addr = redacted["address"][0]
        assert "line" not in addr
        assert "city" not in addr
        assert "postalCode" not in addr
        assert addr["state"] == "PA"
        assert addr["country"] == "US"

    def test_telecom_values_masked(self):
        redacted, stats = redact(CANNED_PATIENT)
        assert all(t["value"] == "***" for t in redacted["telecom"])
        assert stats.telecom_masked == 2

    def test_identifier_hashed(self):
        redacted, stats = redact(CANNED_PATIENT)
        assert redacted["identifier"][0]["value"].startswith("redacted:sha256:")
        assert "MRN-77234-A" not in json.dumps(redacted)
        assert stats.identifiers_hashed == 1

    def test_narrative_div_dropped(self):
        redacted, _ = redact(CANNED_PATIENT)
        assert redacted["text"]["div"] == ""

    def test_gender_preserved(self):
        redacted, _ = redact(CANNED_PATIENT)
        assert redacted["gender"] == "male"


class TestConditionRedaction:

    def test_snomed_code_preserved(self):
        redacted, _ = redact(CANNED_CONDITION)
        assert redacted[0] if isinstance(redacted, list) else redacted  # just ensure shape
        # redact returns (single) dict when given a dict:
        assert redacted["code"]["coding"][0]["code"] == "44054006"

    def test_onset_date_preserved(self):
        redacted, _ = redact(CANNED_CONDITION)
        assert redacted["onsetDateTime"] == "2019-03-14"

    def test_notes_dropped(self):
        redacted, stats = redact(CANNED_CONDITION)
        assert redacted["note"] == []
        assert stats.free_text_dropped >= 1


class TestObservationRedaction:

    def test_loinc_code_preserved(self):
        redacted, _ = redact(CANNED_LABS_BUNDLE)
        first = redacted["entry"][0]["resource"]
        assert first["code"]["coding"][0]["code"] == "4548-4"

    def test_value_quantity_preserved(self):
        redacted, _ = redact(CANNED_LABS_BUNDLE)
        assert redacted["entry"][0]["resource"]["valueQuantity"]["value"] == 6.4

    def test_effective_date_preserved(self):
        redacted, _ = redact(CANNED_LABS_BUNDLE)
        assert redacted["entry"][0]["resource"]["effectiveDateTime"] == "2026-01-15"


class TestGenericShapeRedaction:
    """Non-FHIR flat-dict payloads (HealthEx convenience responses)."""

    def test_patient_name_wiped(self):
        redacted, _ = redact(CANNED_MEDS_FLAT)
        assert redacted[0].get("patientName") is None

    def test_ssn_hashed(self):
        redacted, _ = redact(CANNED_MEDS_FLAT)
        assert redacted[0]["ssn"].startswith("redacted:sha256:")
        assert "123-45-6789" not in json.dumps(redacted)

    def test_rxnorm_preserved(self):
        redacted, _ = redact(CANNED_MEDS_FLAT)
        assert redacted[0]["rxnorm"] == "860975"

    def test_medication_preserved(self):
        redacted, _ = redact(CANNED_MEDS_FLAT)
        assert redacted[0]["medication"] == "Metformin 500mg"


# ---------------------------------------------------------------------------
# Identifier hashing
# ---------------------------------------------------------------------------

class TestIdentifierHashing:

    def test_deterministic_without_salt(self, monkeypatch):
        monkeypatch.delenv("HEALTHCLAW_REDACT_SALT", raising=False)
        p1, _ = redact(CANNED_PATIENT)
        p2, _ = redact(CANNED_PATIENT)
        assert p1["identifier"][0]["value"] == p2["identifier"][0]["value"]

    def test_salt_changes_hash(self, monkeypatch):
        monkeypatch.setenv("HEALTHCLAW_REDACT_SALT", "")
        unsalted, _ = redact(CANNED_PATIENT)
        monkeypatch.setenv("HEALTHCLAW_REDACT_SALT", "rot-2026-04-24")
        salted, _ = redact(CANNED_PATIENT)
        assert unsalted["identifier"][0]["value"] != salted["identifier"][0]["value"]


# ---------------------------------------------------------------------------
# End-to-end: mocked MCP → redacted JSON / NDJSON
# ---------------------------------------------------------------------------

class _CannedSession:
    async def initialize(self):
        return SimpleNamespace(
            serverInfo=SimpleNamespace(name="healthex-mcp-mock", version="0.0.1"),
            protocolVersion="2025-06-18",
        )

    async def call_tool(self, name, arguments):
        canned = {
            "get_health_summary": CANNED_PATIENT,
            "get_conditions": [CANNED_CONDITION],
            "get_labs": CANNED_LABS_BUNDLE,
            "get_medications": CANNED_MEDS_FLAT,
        }
        if name in ("update_records", "check_records_status"):
            text = json.dumps({"status": "ok"})
        elif name in canned:
            text = json.dumps(canned[name])
        else:
            raise RuntimeError(f"unknown tool: {name}")
        return SimpleNamespace(content=[SimpleNamespace(text=text)],
                               structuredContent=None)

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class _CannedTransport:
    async def __aenter__(self): return (None, None, None)
    async def __aexit__(self, *a): return False


@pytest.fixture
def mocked_export():
    """Patch the MCP client symbols inside export_healthex_mcp."""
    import export_healthex_mcp as m
    with patch.object(m, "streamablehttp_client", lambda *a, **kw: _CannedTransport()), \
         patch.object(m, "ClientSession", lambda r, w: _CannedSession()):
        yield m


class TestEndToEndExport:

    def test_single_json_round_trip(self, mocked_export, tmp_path):
        snapshot = asyncio.run(mocked_export._run_export(
            tenant_id="test-tenant", token="fake",
            tools=["get_health_summary", "get_conditions", "get_labs", "get_medications"],
            skip_refresh=False, redact_mode="local",
            healthclaw_url="http://localhost:5000",
        ))
        out = tmp_path / "snap.json"
        size = mocked_export._write_single_json(snapshot, out, pretty=True)
        assert size > 0

        data = json.loads(out.read_text())
        assert "_meta" in data and "records" in data
        assert data["_meta"]["redaction_mode"] == "local"
        assert sum(data["_meta"]["redaction_stats"].values()) > 0

    def test_no_phi_on_disk(self, mocked_export, tmp_path):
        snapshot = asyncio.run(mocked_export._run_export(
            tenant_id="test-tenant", token="fake",
            tools=["get_health_summary", "get_conditions", "get_labs", "get_medications"],
            skip_refresh=True, redact_mode="local",
            healthclaw_url="http://localhost:5000",
        ))
        out = tmp_path / "snap.json"
        mocked_export._write_single_json(snapshot, out, pretty=False)
        text = out.read_text()
        assert "412-555-0199" not in text
        assert "eugene@example.com" not in text
        assert "MRN-77234-A" not in text
        assert "123-45-6789" not in text
        assert "Eugene Vestel" not in text
        # Clinical signal intact across all resource types
        assert "44054006" in text  # SNOMED type-2 diabetes (Condition)
        assert "4548-4" in text    # LOINC A1c (Observation)
        assert "860975" in text    # RxNorm Metformin (non-FHIR shape)

    def test_ndjson_one_line_per_resource(self, mocked_export, tmp_path):
        snapshot = asyncio.run(mocked_export._run_export(
            tenant_id="test-tenant", token="fake",
            tools=["get_labs"],
            skip_refresh=True, redact_mode="local",
            healthclaw_url="http://localhost:5000",
        ))
        out = tmp_path / "snap.ndjson"
        mocked_export._write_ndjson(snapshot, out)

        lines = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
        assert len(lines) >= 3  # meta + at least 2 observations
        assert "_meta" in lines[0]
        obs_lines = [ln for ln in lines[1:] if ln.get("resource_type") == "Observation"]
        assert len(obs_lines) == 2

    def test_no_redact_mode_preserves_phi(self, mocked_export, tmp_path):
        snapshot = asyncio.run(mocked_export._run_export(
            tenant_id="desktop-demo", token="fake",
            tools=["get_health_summary"],
            skip_refresh=True, redact_mode="none",
            healthclaw_url="http://localhost:5000",
        ))
        out = tmp_path / "demo.json"
        mocked_export._write_single_json(snapshot, out, pretty=False)
        # With --no-redact, synthetic PHI stays. This is the escape hatch for
        # demo tenants; tests protect against accidentally flipping the default.
        assert "Eugene" in out.read_text()
        assert sum(snapshot["_meta"]["redaction_stats"].values()) == 0
