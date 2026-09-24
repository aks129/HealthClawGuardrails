"""#377: MedicationStatement is a supported type, end to end.

An EHR that records "what the patient is actually taking" as
MedicationStatement had every one of those resources dropped at ingest. PR
#407 made the drop visible; this makes it stop. The ruling in
docs/2026-08-05-medicationstatement-support-decision.md is followed exactly:
store both, keep them apart, never merge. A statement is never folded into
the MedicationRequest list and never stands in for one.

Every fixture here is synthetic. The names planted in `display` and `text`
are there to prove they do NOT come back out.
"""
from __future__ import annotations

import json

from r6.models import R6Resource

RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
# A name an upstream feed could put in free text. It must never be served.
PLANTED = "Jane Q Synthetic MRN 000-SYN"


def _statement(rid="ms-377-1", subject="Patient/pt-377"):
    return {
        "resourceType": "MedicationStatement",
        "id": rid,
        "status": "recorded",
        "subject": {"reference": subject},
        "medicationCodeableConcept": {
            "text": f"metformin per {PLANTED}",
            "coding": [{"system": RXNORM, "code": "860975",
                        "display": f"{PLANTED} metformin"}],
        },
        "note": [{"text": f"taken as told by {PLANTED}"}],
    }


# --- the type itself ---------------------------------------------------------

def test_medication_statement_is_a_supported_type():
    """MUTATION: drop 'MedicationStatement' from SUPPORTED_TYPES -> red."""
    assert R6Resource.is_supported_type("MedicationStatement")


def test_the_validator_checks_status_medication_and_subject():
    """Parity with MedicationRequest, minus `intent`, which a statement does
    not have. MUTATION: drop the validator branch -> red."""
    from r6.validator import R6Validator

    v = R6Validator()
    ok = v.validate_resource(_statement())
    assert ok["valid"], ok
    bad = v.validate_resource({"resourceType": "MedicationStatement"})
    exprs = {e for i in bad["operation_outcome"]["issue"]
             for e in i.get("expression", [])}
    assert {"MedicationStatement.status",
            "MedicationStatement.medication[x]",
            "MedicationStatement.subject"} <= exprs, bad
    assert not bad["valid"]


def test_a_direct_statement_write_needs_human_confirmation(client, auth_headers):
    """Supporting the type opens the generic create route to it. Without
    CLINICAL_RESOURCE_TYPES it would take a step-up token alone — a weaker
    gate than the MedicationRequest beside it. MUTATION: drop it from
    CLINICAL_RESOURCE_TYPES -> red (201)."""
    resp = client.post("/r6/fhir/MedicationStatement",
                       data=json.dumps(_statement("ms-377-write")),
                       content_type="application/json", headers=auth_headers)
    assert resp.status_code != 201, resp.get_data(as_text=True)
    assert "human confirmation" in resp.get_data(as_text=True)


# --- every ingest path keeps it ----------------------------------------------

def test_fasten_ingest_stores_it_instead_of_skipping_it(app, caplog):
    from tests.test_ingest_resilience import _run_fasten_ingest

    job, detail = _run_fasten_ingest(
        app, "ms-377-fasten", [_statement("ms-377-fasten")], caplog)
    assert job.ingested_resources == 1, detail
    assert job.skipped_resources == 0
    assert "MedicationStatement" not in detail, (
        "a stored type is being reported as skipped")
    with app.app_context():
        row = R6Resource.query.filter_by(
            tenant_id="test-tenant", resource_type="MedicationStatement",
            id="ms-377-fasten").first()
        assert row is not None


def test_the_shc_bundle_path_stores_it(app, tenant_id):
    from r6.shc import routes as shc

    counts = shc._ingest_bundle(app, [_statement("ms-377-shc")],
                                tenant_id, "flexpa", "job377ms")
    assert counts["ingested"] == 1 and counts["skipped"] == 0, counts
    assert counts["skipped_types"] == {}


def test_ingest_context_stores_it(client, tenant_headers):
    resp = client.post(
        "/r6/fhir/Bundle/$ingest-context",
        data=json.dumps({"resourceType": "Bundle", "type": "collection",
                         "entry": [
                             {"resource": {"resourceType": "Patient",
                                           "id": "pt-377"}},
                             {"resource": _statement("ms-377-ctx")}]}),
        content_type="application/json", headers=tenant_headers)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["resource_count"] == 2
    assert body["skipped_count"] == 0


# --- the guarded read path ---------------------------------------------------

def _seed(app, tenant, rid):
    from r6.shc import routes as shc
    counts = shc._ingest_bundle(app, [_statement(rid)], tenant, "flexpa",
                                f"seed-{rid}")
    assert counts["ingested"] == 1, counts


def _assert_redacted_and_relabelled(res):
    served = json.dumps(res)
    assert PLANTED not in served, (
        "upstream display/text reached the reader — the non-negotiable "
        "(CLAUDE.md): labels come from r6/terminology.py, never the feed")
    concept = res["medicationCodeableConcept"]
    assert concept["coding"][0]["display"] == "Metformin 500 mg", (
        "the server-owned label, keyed by code, is not being applied")
    assert concept["text"] == "Metformin 500 mg"


def test_read_is_redacted_then_labelled_by_code(app, client, tenant_headers,
                                                 tenant_id):
    _seed(app, tenant_id, "ms-377-read")
    resp = client.get("/r6/fhir/MedicationStatement/ms-377-read",
                      headers=tenant_headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    _assert_redacted_and_relabelled(resp.get_json())


def test_search_finds_it_by_patient_redacted(app, client, tenant_headers,
                                              tenant_id):
    _seed(app, tenant_id, "ms-377-search")
    resp = client.get("/r6/fhir/MedicationStatement?patient=Patient/pt-377",
                      headers=tenant_headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    found = [e["resource"] for e in resp.get_json().get("entry", [])
             if e["resource"].get("resourceType") == "MedicationStatement"]
    assert [r["id"] for r in found] == ["ms-377-search"]
    _assert_redacted_and_relabelled(found[0])


def test_another_tenant_cannot_read_it(app, client, other_tenant_headers,
                                       tenant_id):
    _seed(app, tenant_id, "ms-377-iso")
    resp = client.get("/r6/fhir/MedicationStatement/ms-377-iso",
                      headers=other_tenant_headers)
    assert resp.status_code == 404


# --- the agent reads it, apart from the requests -----------------------------

class _HC:
    """Fake HealthClaw client: proves the calls are MADE, not accepted."""

    def __init__(self):
        self.searched: list[str] = []
        self.reads: list[str] = []

    def search(self, tenant, rt, params=None):
        self.searched.append(rt)
        if rt == "MedicationStatement":
            return {"entry": [{"resource": {
                "resourceType": "MedicationStatement", "id": "s1",
                "status": "recorded",
                "medicationReference": {"reference": "Medication/m1"}}}]}
        if rt == "MedicationRequest":
            return {"entry": [{"resource": {
                "resourceType": "MedicationRequest", "id": "r1",
                "status": "active",
                "medicationCodeableConcept": {"text": "Lisinopril 10 mg"}}}]}
        return {"entry": []}

    def read(self, tenant, rt, rid):
        self.reads.append(f"{rt}/{rid}")
        return {"resourceType": "Medication", "id": rid,
                "code": {"text": "Atorvastatin 20 mg"}}


def test_search_records_offers_it():
    from careagents.agent import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "search_records")
    enum = tool["parameters"]["properties"]["resource_type"]["enum"]
    assert "MedicationStatement" in enum


def test_the_synced_record_count_includes_statements():
    """COUNTED_TYPES is what the patient can reach; a statement now is.
    A record count, not a medication count, so no dedup question arises."""
    from careagents.healthclaw import HealthClawClient

    assert "MedicationStatement" in HealthClawClient.COUNTED_TYPES


def test_health_summary_keeps_statements_apart_from_requests():
    """Store both, keep them apart, never merge (the #377 ruling). A merged
    list needs a dedup rule the decision doc says code cannot supply.
    MUTATION: fold statements into "medications" -> red."""
    from careagents.agent import _execute_tool

    hc = _HC()
    out = json.loads(_execute_tool(hc, "t", "get_health_summary", {}, []))
    assert [m["type"] for m in out["medications"]] == ["MedicationRequest"]
    assert [m["type"] for m in out["medication_statements"]] == [
        "MedicationStatement"]
    assert out["medication_statements"][0]["name"] == "Atorvastatin 20 mg", (
        "a statement's medicationReference was not chased")
    assert hc.reads == ["Medication/m1"]


def test_search_records_chases_a_statement_reference():
    from careagents.agent import _execute_tool

    hc = _HC()
    out = json.loads(_execute_tool(hc, "t", "search_records",
                                   {"resource_type": "MedicationStatement"},
                                   []))
    assert out[0]["name"] == "Atorvastatin 20 mg"
