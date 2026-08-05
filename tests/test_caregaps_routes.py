# tests/test_caregaps_routes.py
import json

from r6.models import R6Resource, db


def _store(app, resource, tenant_id):
    with app.app_context():
        db.session.add(R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            resource_id=resource.get("id"),
            tenant_id=tenant_id))
        db.session.commit()


def _seed_patient(app, tenant_id, pid="p1", gender="female", birth="1968-05-01"):
    _store(app, {"resourceType": "Patient", "id": pid, "gender": gender,
                 "birthDate": birth}, tenant_id)
    _store(app, {"resourceType": "Observation", "id": f"o-{pid}", "status": "final",
                 "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
                 "subject": {"reference": f"Patient/{pid}"},
                 "effectiveDateTime": "2026-03-01"}, tenant_id)


def _resp_param(body, name):
    for p in body["parameter"]:
        if p["name"] == name:
            return p
    return None


def test_care_gaps_returns_parameters_with_summary(client, app, tenant_id, tenant_headers):
    _seed_patient(app, tenant_id)
    r = client.post("/r6/fhir/Patient/$care-gaps?subject=Patient/p1",
                    headers=tenant_headers)
    assert r.status_code == 200
    body = r.get_json()
    assert body["resourceType"] == "Parameters"
    summary = json.loads(_resp_param(body, "summary")["valueString"])
    assert summary["total"] > 0
    assert "bp-screening" not in [g["rule_id"] for g in summary["gaps"]]
    consumer = json.loads(_resp_param(body, "consumerSummary")["valueString"])
    assert "lines" in consumer
    detail = json.loads(_resp_param(body, "detail")["valueString"])
    assert isinstance(detail, list) and len(detail) == summary["total"]
    assert _resp_param(body, "disclaimer") is not None


def test_care_gaps_get_also_works(client, app, tenant_id, tenant_headers):
    _seed_patient(app, tenant_id, pid="p2")
    r = client.get("/r6/fhir/Patient/$care-gaps?subject=Patient/p2",
                   headers=tenant_headers)
    assert r.status_code == 200
    assert r.get_json()["resourceType"] == "Parameters"


def test_care_gaps_requires_tenant(client):
    r = client.post("/r6/fhir/Patient/$care-gaps?subject=Patient/p1")
    assert r.status_code == 400


def test_care_gaps_unknown_patient_is_ok_but_indeterminate(client, tenant_headers):
    r = client.post("/r6/fhir/Patient/$care-gaps?subject=Patient/does-not-exist",
                    headers=tenant_headers)
    assert r.status_code == 200
    summary = json.loads(_resp_param(r.get_json(), "summary")["valueString"])
    # No patient found -> no birthDate/gender available -> indeterminate/not-run rules
    assert summary["total"] >= 0


# ─────────────────────────────────────────────
# The production call shape: no subject at all
# ─────────────────────────────────────────────

def test_care_gaps_with_no_subject_uses_the_tenants_own_patient(
        client, app, tenant_id, tenant_headers):
    """The shape both production callers actually send (#389).

    CareAgents' get_care_gaps and the care-gaps MCP App page post an empty
    body with no ?subject=. Every other test in this file passes one, which
    is why this survived: with subject None the resource filter compares
    every subject.reference against None, nothing matches, and the evaluator
    is handed an empty record.

    MUTATION: delete the fallback branch in _resolve_subject (return the
    supplied subject unconditionally) -> both asserts red.
    """
    _seed_patient(app, tenant_id, pid="p-solo")
    _store(app, {"resourceType": "Condition", "id": "c-dm",
                 "subject": {"reference": "Patient/p-solo"},
                 "code": {"coding": [{"system": "http://snomed.info/sct",
                                      "code": "44054006"}]}}, tenant_id)

    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers,
                    json={})
    assert r.status_code == 200
    body = r.get_json()
    resolution = json.loads(_resp_param(body, "subjectResolution")["valueString"])
    assert resolution == {"state": "tenant-default", "subject": "Patient/p-solo"}

    # The tenant's own Condition reached the evaluator: the diabetes rule is
    # no longer dismissed as "applies to patients with a diabetes diagnosis".
    # Against subject None it was, because the Condition never matched.
    detail = json.loads(_resp_param(body, "detail")["valueString"])
    a1c = next(d for d in detail if d["rule_id"] == "diabetes-a1c")
    assert a1c["status"] != "not_applicable", (
        "the tenant's stored Condition did not reach the evaluator")


def test_care_gaps_with_no_patient_row_says_so_rather_than_no_gaps(
        client, tenant_headers):
    """"Could not look" must not arrive as "looked and found none".

    Third instance of this shape in a week (#379 medications, #381 the
    brief's care gaps, #390 the intake form), and the one on the highest
    traffic entry point.

    MUTATION: collapse the unresolved reason (return a bare empty consumer
    summary) -> red.
    """
    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers,
                    json={})
    assert r.status_code == 200
    body = r.get_json()
    resolution = json.loads(_resp_param(body, "subjectResolution")["valueString"])
    assert resolution == {"state": "no-patient", "subject": None}
    consumer = json.loads(_resp_param(body, "consumerSummary")["valueString"])
    assert consumer["lines"] == []
    assert consumer["unevaluated"] == "no-patient"
    assert "no screenings outstanding" in consumer["unevaluated_note"]


def test_care_gaps_with_two_patient_rows_is_ambiguous_not_empty(
        client, app, tenant_id, tenant_headers):
    """Two Patient rows and no subject: the fallback cannot pick one. That is
    its own outcome, not a clean sheet.

    MUTATION: take rows[0] instead of reporting ambiguity -> red.
    """
    _seed_patient(app, tenant_id, pid="p-one")
    _seed_patient(app, tenant_id, pid="p-two")
    r = client.post("/r6/fhir/Patient/$care-gaps", headers=tenant_headers,
                    json={})
    assert r.status_code == 200
    body = r.get_json()
    resolution = json.loads(_resp_param(body, "subjectResolution")["valueString"])
    assert resolution == {"state": "ambiguous-patient", "subject": None}
    consumer = json.loads(_resp_param(body, "consumerSummary")["valueString"])
    assert consumer["unevaluated"] == "ambiguous-patient"


def test_care_gaps_with_a_supplied_subject_reports_that_state(
        client, app, tenant_id, tenant_headers):
    """A caller-supplied subject is not the fallback, and says so."""
    _seed_patient(app, tenant_id, pid="p-named")
    r = client.get("/r6/fhir/Patient/$care-gaps?subject=Patient/p-named",
                   headers=tenant_headers)
    resolution = json.loads(
        _resp_param(r.get_json(), "subjectResolution")["valueString"])
    assert resolution == {"state": "supplied", "subject": "Patient/p-named"}


# ─────────────────────────────────────────────
# MCP App page (embedded HTML surface)
# ─────────────────────────────────────────────

class TestCareGapsMcpApp:
    """The care-gaps MCP App page — layout ported from SmartHealthConnect
    (archived), data path rebuilt on the engine's own $care-gaps operation."""

    def test_serves_html_with_mcp_app_profile(self, client):
        resp = client.get('/r6/fhir/mcp-apps/care-gaps/?tenant_id=desktop-demo')
        assert resp.status_code == 200
        assert 'text/html' in resp.headers['Content-Type']
        assert 'profile=mcp-app' in resp.headers['Content-Type']
        assert resp.headers.get('X-MCP-App') == 'care-gaps'
        body = resp.get_data(as_text=True)
        assert '<title>Care Gaps' in body
        assert 'desktop-demo' in body

    def test_page_reads_through_the_guarded_operation_only(self, client):
        """The page's only data path is the engine's $care-gaps operation —
        no direct table reads, no alternate endpoints (the SHC failure mode)."""
        body = client.get('/r6/fhir/mcp-apps/care-gaps/').get_data(as_text=True)
        assert '/r6/fhir/Patient/$care-gaps' in body
        # no other fetch targets appear in the page
        import re
        fetches = re.findall(r"fetch\('([^']+)'", body)
        assert fetches == ['/r6/fhir/Patient/$care-gaps']

    def test_no_tenant_renders_empty_shell(self, client):
        resp = client.get('/r6/fhir/mcp-apps/care-gaps/')
        assert resp.status_code == 200
        assert 'Enter a tenant id' in resp.get_data(as_text=True)
