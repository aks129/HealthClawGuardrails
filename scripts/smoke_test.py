#!/usr/bin/env python3
"""Smoke test for the HealthEx export + HealthClaw redaction pipeline.

Runs without real credentials. Monkey-patches the MCP session with a canned
clinical payload carrying synthetic PHI, exercises both redaction modes and
both output formats, and asserts the redaction rules held.

Usage:
    python scripts/smoke_test.py

Exit code 0 on all green, 1 on any failure.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# Let us import siblings regardless of CWD.
sys.path.insert(0, str(Path(__file__).parent))

from healthclaw_redact import redact  # noqa: E402

# Canned payload. Mix of FHIR resources and HealthEx-style dicts, all synthetic.
CANNED_PAYLOADS = {
    "get_health_summary": {
        "resourceType": "Patient",
        "id": "pat-001",
        "name": [{"given": ["Eugene"], "family": "Vestel", "text": "Eugene Vestel"}],
        "birthDate": "1981-05-29",
        "gender": "male",
        "address": [{
            "line": ["123 Main St"],
            "city": "Pittsburgh",
            "state": "PA",
            "postalCode": "15228",
            "country": "US",
        }],
        "telecom": [
            {"system": "phone", "value": "412-555-0199"},
            {"system": "email", "value": "eugene@example.com"},
        ],
        "identifier": [
            {"system": "http://hospitals.example/mrn", "value": "MRN-77234-A"},
        ],
        "text": {"status": "generated", "div": "<div>Eugene Vestel, 44yo male</div>"},
    },
    "get_conditions": [
        {
            "resourceType": "Condition",
            "id": "cond-001",
            "subject": {"reference": "Patient/pat-001"},
            "code": {"coding": [
                {"system": "http://snomed.info/sct", "code": "44054006",
                 "display": "Diabetes mellitus type 2"},
            ]},
            "onsetDateTime": "2019-03-14",
            "note": [{"text": "Patient Eugene Vestel reports worsening symptoms."}],
        },
    ],
    "get_labs": {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {"resource": {
                "resourceType": "Observation",
                "id": "obs-001",
                "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4",
                                     "display": "Hemoglobin A1c"}]},
                "valueQuantity": {"value": 6.4, "unit": "%"},
                "effectiveDateTime": "2026-01-15",
                "subject": {"reference": "Patient/pat-001"},
            }},
            {"resource": {
                "resourceType": "Observation",
                "id": "obs-002",
                "code": {"coding": [{"system": "http://loinc.org", "code": "2085-9",
                                     "display": "HDL Cholesterol"}]},
                "valueQuantity": {"value": 52, "unit": "mg/dL"},
                "effectiveDateTime": "2026-01-15",
            }},
        ],
    },
    "get_medications": [
        {
            "patientName": "Eugene Vestel",
            "ssn": "123-45-6789",
            "medication": "Metformin 500mg",
            "rxnorm": "860975",
            "startDate": "2019-03-15",
        },
    ],
}


class _CannedSession:
    """Stand-in for mcp.ClientSession. Returns canned content blocks."""

    async def initialize(self):
        return SimpleNamespace(
            serverInfo=SimpleNamespace(name="healthex-mcp-mock", version="0.0.1-test"),
            protocolVersion="2025-06-18",
        )

    async def call_tool(self, name: str, arguments: dict):
        if name in ("update_records", "check_records_status"):
            text = json.dumps({"status": "ok", "last_sync": "2026-04-24T00:00:00Z"})
            return SimpleNamespace(
                content=[SimpleNamespace(text=text)],
                structuredContent=None,
            )
        if name not in CANNED_PAYLOADS:
            raise RuntimeError(f"unknown tool: {name}")
        text = json.dumps(CANNED_PAYLOADS[name])
        return SimpleNamespace(
            content=[SimpleNamespace(text=text)],
            structuredContent=None,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _CannedTransport:
    async def __aenter__(self):
        return (None, None, None)

    async def __aexit__(self, *a):
        return False


def _install_mocks():
    """Replace MCP client symbols in the export module with canned versions."""
    import export_healthex_mcp as m

    def _fake_streamablehttp_client(url, headers=None):
        return _CannedTransport()

    def _fake_ClientSession(read, write):
        return _CannedSession()

    m.streamablehttp_client = _fake_streamablehttp_client
    m.ClientSession = _fake_ClientSession


def _assert(condition: bool, label: str, failures: list[str]) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def _run_unit_redaction_tests(failures: list[str]) -> None:
    print("[unit] healthclaw_redact")
    patient = CANNED_PAYLOADS["get_health_summary"]
    redacted, stats = redact(patient)

    _assert(redacted["name"][0].get("text") == "E. V.",
            "Patient.name truncated to initials", failures)
    _assert("given" not in redacted["name"][0] and "family" not in redacted["name"][0],
            "Patient.name given/family dropped", failures)
    _assert(redacted["birthDate"] == "1981",
            "Patient.birthDate coarsened to year", failures)
    _assert("line" not in redacted["address"][0] and "postalCode" not in redacted["address"][0],
            "Patient.address line/postalCode dropped", failures)
    _assert(redacted["address"][0].get("state") == "PA",
            "Patient.address state preserved", failures)
    _assert(all(t["value"] == "***" for t in redacted["telecom"]),
            "Patient.telecom values masked", failures)
    _assert(redacted["identifier"][0]["value"].startswith("redacted:sha256:"),
            "Patient.identifier.value hashed", failures)
    _assert(redacted["text"]["div"] == "",
            "Patient.text.div dropped", failures)
    _assert(redacted.get("gender") == "male",
            "Patient.gender preserved", failures)
    _assert(stats.names_truncated == 1, "stats: names_truncated == 1", failures)
    _assert(stats.addresses_stripped == 1, "stats: addresses_stripped == 1", failures)
    _assert(stats.identifiers_hashed == 1, "stats: identifiers_hashed == 1", failures)
    _assert(stats.telecom_masked == 2, "stats: telecom_masked == 2", failures)
    _assert(stats.birthdates_coarsened == 1, "stats: birthdates_coarsened == 1", failures)

    # Conditions: clinical codes must survive, free-text note must not.
    conditions = CANNED_PAYLOADS["get_conditions"]
    cond_redacted, cond_stats = redact(conditions)
    _assert(cond_redacted[0]["code"]["coding"][0]["code"] == "44054006",
            "Condition SNOMED code preserved", failures)
    _assert(cond_redacted[0]["onsetDateTime"] == "2019-03-14",
            "Condition onsetDateTime preserved", failures)
    _assert(cond_redacted[0]["note"] == [],
            "Condition.note free-text dropped", failures)

    # Labs: LOINC codes + values + effectiveDateTime must survive.
    labs = CANNED_PAYLOADS["get_labs"]
    lab_redacted, _ = redact(labs)
    first_obs = lab_redacted["entry"][0]["resource"]
    _assert(first_obs["code"]["coding"][0]["code"] == "4548-4",
            "Observation LOINC code preserved", failures)
    _assert(first_obs["valueQuantity"]["value"] == 6.4,
            "Observation valueQuantity preserved", failures)
    _assert(first_obs["effectiveDateTime"] == "2026-01-15",
            "Observation effectiveDateTime preserved", failures)

    # Non-FHIR shape: generic PHI keys get wiped.
    meds = CANNED_PAYLOADS["get_medications"]
    meds_redacted, meds_stats = redact(meds)
    _assert(meds_redacted[0].get("patientName") is None,
            "generic patientName wiped", failures)
    _assert(meds_redacted[0]["ssn"].startswith("redacted:sha256:"),
            "generic ssn hashed", failures)
    _assert(meds_redacted[0]["medication"] == "Metformin 500mg",
            "clinical fields preserved in non-FHIR shape", failures)
    _assert(meds_redacted[0]["rxnorm"] == "860975",
            "RxNorm preserved in non-FHIR shape", failures)
    _assert(meds_stats.generic_keys_redacted >= 2,
            "stats: generic_keys_redacted >= 2", failures)


def _run_integration_test(failures: list[str]) -> None:
    print("[integration] single-JSON export")
    _install_mocks()
    import export_healthex_mcp as m

    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "snap.json"
        snapshot = asyncio.run(m._run_export(
            tenant_id="test-tenant",
            token="fake-token",
            tools=list(CANNED_PAYLOADS.keys()),
            skip_refresh=False,
            redact_mode="local",
            healthclaw_url="http://localhost:5000",
        ))
        size = m._write_single_json(snapshot, out, pretty=True)
        _assert(size > 0, "single JSON file written", failures)

        parsed = json.loads(out.read_text())
        _assert("_meta" in parsed and "records" in parsed,
                "snapshot has _meta and records blocks", failures)
        stats = parsed["_meta"]["redaction_stats"]
        _assert(sum(stats.values()) > 0,
                "redaction stats non-zero after export", failures)
        _assert(parsed["_meta"]["redaction_mode"] == "local",
                "redaction_mode recorded as local", failures)

        text_blob = json.dumps(parsed)
        _assert("412-555-0199" not in text_blob,
                "phone number absent from exported blob", failures)
        _assert("eugene@example.com" not in text_blob,
                "email absent from exported blob", failures)
        _assert("MRN-77234-A" not in text_blob,
                "MRN absent from exported blob", failures)
        _assert("123-45-6789" not in text_blob,
                "SSN absent from exported blob", failures)
        _assert("Eugene Vestel" not in text_blob,
                "full name absent from exported blob", failures)
        _assert("44054006" in text_blob,
                "SNOMED code still present in exported blob", failures)

    print("[integration] NDJSON export")
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "snap.ndjson"
        snapshot = asyncio.run(m._run_export(
            tenant_id="test-tenant",
            token="fake-token",
            tools=list(CANNED_PAYLOADS.keys()),
            skip_refresh=True,
            redact_mode="local",
            healthclaw_url="http://localhost:5000",
        ))
        m._write_ndjson(snapshot, out)

        lines = [ln for ln in out.read_text().splitlines() if ln.strip()]
        _assert(len(lines) >= 2, "NDJSON has multiple lines", failures)
        for i, line in enumerate(lines):
            try:
                json.loads(line)
            except json.JSONDecodeError as e:
                _assert(False, f"line {i} parseable ({e})", failures)
                break
        else:
            _assert(True, "every NDJSON line is valid JSON", failures)

        first = json.loads(lines[0])
        _assert("_meta" in first, "NDJSON first line is _meta", failures)

        obs_lines = [json.loads(ln) for ln in lines[1:]
                     if '"resource_type": "Observation"' in ln]
        _assert(len(obs_lines) >= 2,
                "NDJSON emits one line per Observation in Bundle", failures)


def main() -> int:
    failures: list[str] = []
    _run_unit_redaction_tests(failures)
    _run_integration_test(failures)
    print()
    if failures:
        print(f"FAIL: {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
