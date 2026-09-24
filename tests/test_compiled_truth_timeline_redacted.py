"""`$compiled-truth` builds its timeline from redacted Provenance.

The timeline used to read the stored rows raw, so `agent[].who.display`,
`reason[0].coding[0].display` and the curatr-correction strings reached the
agent verbatim while `GET /Provenance/<id>` stripped all four (found by
tests/test_phi_canary_leak_scan.py). Each row now goes through
`apply_redaction` first, and labels come back only from r6/terminology.py.

Why the two curatr-correction strings are stripped rather than kept:

- `patient_intent` is the patient's own words (the MCP tool's input, the
  `$curatr-apply-fix` body), free text by construction.
- `change_summary` is our grammar when `r6.curatr.apply_fix` writes it —
  normalised paths joined with " updated" — but the timeline reads whatever
  Provenance the tenant holds, and a Provenance carrying the same extension
  arrives through `$ingest-context` with any text in it. The read cannot
  tell the two apart, so the grammar proves nothing at read time.
  `test_an_ingested_curatr_extension_is_not_ours` pins that.
"""
import json

import pytest

from models import db
from r6.curatr import apply_fix
from r6.models import R6Resource
from r6.stepup import generate_step_up_token

RID = "ct-cond-1"
INTENT = "ZZINTENT my neighbour Dr Quill said it cleared up"
FORGED = "ZZFORGED Jane Roe, MRN 55501, told us at home"


def _timeline(client, tenant_id, rtype="Condition", rid=RID):
    r = client.get(f"/r6/fhir/{rtype}/{rid}/$compiled-truth",
                   headers={"X-Tenant-Id": tenant_id})
    assert r.status_code == 200, r.get_data(as_text=True)
    params = {p["name"]: p for p in r.get_json()["parameter"]}
    events = [{q["name"]: q["valueString"] for q in e["part"]}
              for e in params["timeline"]["part"]]
    return events, r.get_data(as_text=True)


@pytest.fixture
def fixed(app, client, tenant_id):
    with app.app_context():
        db.session.add(R6Resource("Condition", json.dumps({
            "resourceType": "Condition", "id": RID,
            "subject": {"reference": "Patient/alice"},
            "code": {"coding": [{"system": "http://snomed.info/sct",
                                 "code": "44054006"}]},
            "clinicalStatus": {"coding": [{"code": "active"}]}}),
            resource_id=RID, tenant_id=tenant_id))
        db.session.commit()
        result = apply_fix(
            "Condition", RID,
            [{"field_path": "Condition.clinicalStatus.coding[0].code",
              "new_value": "resolved"}],
            INTENT, tenant_id)
    assert "error" not in result, result
    return result


def test_a_curatr_fix_shows_in_the_timeline_without_free_text(
        client, tenant_id, fixed):
    events, body = _timeline(client, tenant_id)
    assert len(events) == 1
    ev = events[0]
    assert ev["provenance_id"] == fixed["provenance"]["id"]
    assert ev["recorded"] == fixed["provenance"]["recorded"]
    # The patient's words and the stored summary do not come back ...
    assert ev["patient_intent"] == ""
    assert ev["summary"] == ""
    assert "ZZINTENT" not in body
    assert fixed["change_summary"] not in body
    # ... nor the stored who.display, which has no code to relabel from.
    assert ev["agent"] == "system"
    assert "HealthClaw Curatr" not in body
    # The reason is relabelled from r6/terminology.py by its code, not the
    # "patient administration" display curatr stored.
    assert ev["reason"] == "Patient administration"


def test_an_ingested_curatr_extension_is_not_ours(client, tenant_id):
    """The proof that change_summary cannot be kept on its grammar: the same
    extension URL reaches the store from a client with any text in it, and
    `$compiled-truth` must not return it."""
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "Condition", "id": RID,
                      "subject": {"reference": "Patient/alice"},
                      "code": {"coding": [{"code": "x"}]}}},
        {"resource": {
            "resourceType": "Provenance", "id": "ct-prov-forged",
            "target": [{"reference": f"Condition/{RID}"}],
            "recorded": "2026-01-15T00:00:00Z",
            "agent": [{"who": {"display": FORGED}}],
            "extension": [{
                "url": "https://healthclaw.example.org/fhir/"
                       "StructureDefinition/curatr-correction",
                "extension": [
                    {"url": "change_summary", "valueString": FORGED},
                    {"url": "patient_intent", "valueString": FORGED}]}]}},
    ]}
    r = client.post("/r6/fhir/Bundle/$ingest-context", json=bundle,
                    headers={"X-Tenant-Id": tenant_id,
                             "X-Step-Up-Token": generate_step_up_token(
                                 tenant_id)})
    assert r.status_code == 201, r.get_data(as_text=True)
    events, body = _timeline(client, tenant_id)
    assert [e["provenance_id"] for e in events] == ["ct-prov-forged"]
    assert "ZZFORGED" not in body
    assert events[0]["summary"] == events[0]["patient_intent"] == ""
