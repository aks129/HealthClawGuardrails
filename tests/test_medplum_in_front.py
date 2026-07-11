"""HealthClaw-in-front-of-Medplum: proves the guardrail stack (PHI redaction,
audit, step-up) wraps a real MedplumProxy. Only the upstream HTTP + OAuth token
are mocked — the MedplumProxy logic and the FHIR route guardrails are real.
"""
import json
from unittest.mock import patch, MagicMock

from r6.fhir_proxy import MedplumProxy
from r6.models import AuditEventRecord


UNREDACTED_MEDPLUM_PATIENT = {
    "resourceType": "Patient", "id": "pt-medplum",
    "name": [{"family": "Hernandez", "given": ["Rosa"]}],
    "identifier": [{"system": "http://hl7.org/fhir/sid/us-ssn",
                    "value": "123-45-6789"}],
    "telecom": [{"system": "phone", "value": "617-555-0199"}],
    "address": [{"line": ["42 Real St"], "city": "Boston"}],
    "contact": [{"name": {"family": "Hernandez", "given": ["Miguel"]},
                 "telecom": [{"system": "phone", "value": "617-555-0142"}]}],
    "birthDate": "1980-07-04",
}


def _medplum_proxy_returning(resource, status=200):
    proxy = MedplumProxy("https://api.medplum.com/fhir/R4", "cid", "secret")
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = resource
    proxy._client.get = MagicMock(return_value=resp)
    proxy._client.post = MagicMock(return_value=resp)
    return proxy


def test_guardrails_redact_and_audit_medplum_read(client, tenant_headers,
                                                  tenant_id, app):
    proxy = _medplum_proxy_returning(UNREDACTED_MEDPLUM_PATIENT)
    with patch("r6.routes.get_proxy_for_request", return_value=proxy):
        resp = client.get("/r6/fhir/Patient/pt-medplum", headers=tenant_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    blob = json.dumps(body)

    # PHI redaction applied to the Medplum-returned resource
    assert body["name"][0]["family"] == "H."                 # name truncated
    assert body["telecom"][0]["value"] == "[Redacted]"       # phone masked
    ident = body["identifier"][0]["value"]
    assert ident.startswith("***") and ident.endswith("6789")  # SSN masked
    assert "42 Real St" not in blob                          # address line gone
    assert "617-555-0142" not in blob                        # emergency contact gone
    assert body.get("_source") == "upstream"                 # came from Medplum

    # Immutable audit recorded for the Medplum-backed read
    with app.app_context():
        n = AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_type="Patient",
            event_type="read").count()
        assert n >= 1

    proxy._client.get.assert_called_once()


def test_medplum_write_gated_by_step_up_before_upstream(client, tenant_headers):
    proxy = _medplum_proxy_returning({"resourceType": "Patient", "id": "x"}, 201)
    with patch("r6.routes.get_proxy_for_request", return_value=proxy):
        resp = client.post("/r6/fhir/Patient", headers=tenant_headers,
                           json={"resourceType": "Patient",
                                 "name": [{"family": "Test"}]})
    # No step-up token -> blocked by the guardrail BEFORE any Medplum call
    assert resp.status_code == 401
    proxy._client.post.assert_not_called()


# --- Error fidelity through the facade (#74) --------------------------------
# A Medplum rejection must surface as a sanitized error with its real status,
# never as an empty result set or a fake 404 — and it must be audited as a
# failure, with no PHI or internal URLs transiting the guardrail boundary.

MEDPLUM_REJECTION_WITH_LEAKY_DIAGNOSTICS = {
    "resourceType": "OperationOutcome",
    "issue": [{
        "severity": "error", "code": "invalid",
        "details": {"text": "Unknown search parameter: datetime"},
        # Neither the patient name nor the internal URL may transit
        "diagnostics": ("while searching for Rosa Hernandez see "
                        "https://db.internal:5432/trace/8842"),
    }],
}


def test_medplum_search_rejection_surfaces_not_empty_bundle(client, tenant_headers,
                                                            tenant_id, app):
    proxy = _medplum_proxy_returning(MEDPLUM_REJECTION_WITH_LEAKY_DIAGNOSTICS, 400)
    with patch("r6.routes.get_proxy_for_request", return_value=proxy):
        resp = client.get("/r6/fhir/Observation?datetime=ge2024-01-01",
                          headers=tenant_headers)

    # The rejection surfaces with its real status and a machine-readable code
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["code"] == "invalid"
    # ... the message is synthesized, and NOTHING from the upstream body
    # transits (not the corrective text, not the planted name/URL)
    blob = json.dumps(body)
    assert "Unknown search parameter" not in blob
    assert "Rosa Hernandez" not in blob
    assert "db.internal" not in blob

    # Audited as a failure, not a zero-result success
    with app.app_context():
        rec = AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_type="Observation",
            event_type="read", outcome="failure").first()
        assert rec is not None
        assert "rejected HTTP 400" in rec.detail


def test_medplum_auth_failure_read_is_not_a_404_and_is_audited(client, tenant_headers,
                                                               tenant_id, app):
    proxy = _medplum_proxy_returning(
        {"resourceType": "OperationOutcome",
         "issue": [{"severity": "error", "code": "login",
                    "details": {"text": "Invalid access token"}}]}, 401)
    with patch("r6.routes.get_proxy_for_request", return_value=proxy):
        resp = client.get("/r6/fhir/Patient/pt-medplum", headers=tenant_headers)

    # NOT "Patient/pt-medplum not found" — the resource may well exist
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["code"] == "security"
    assert "not found" not in json.dumps(body)

    # The failed access attempt is visible in the audit trail
    with app.app_context():
        rec = AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_type="Patient",
            event_type="read", outcome="failure").first()
        assert rec is not None
        assert "HTTP 502" in rec.detail


def test_medplum_search_preserves_and_sanitizes_outcome_warning_entries(client, tenant_headers):
    bundle = {
        "resourceType": "Bundle", "type": "searchset", "total": 1,
        "entry": [
            {"fullUrl": "https://api.medplum.com/fhir/R4/Observation/obs-1",
             "resource": {"resourceType": "Observation", "id": "obs-1",
                          "status": "final", "code": {"text": "A1c"}},
             "search": {"mode": "match"}},
            {"resource": {"resourceType": "OperationOutcome",
                          "issue": [{"severity": "warning", "code": "not-supported",
                                     "details": {"text": "ignored parameter foo"},
                                     # neither may transit through a 200 bundle
                                     "diagnostics": ("Rosa Hernandez trace at "
                                                     "https://db.internal:5432/q")}]},
             # search carries an extension AND nested objects under the
             # allowlisted mode/score keys — only scalar mode/score survive
             "search": {"mode": "outcome",
                        "score": {"nested": "Rosa Hernandez"},
                        "extension": [{"url": "https://internal/ext", "valueString": "x"}]}},
        ],
    }
    proxy = _medplum_proxy_returning(bundle, 200)
    with patch("r6.routes.get_proxy_for_request", return_value=proxy):
        resp = client.get("/r6/fhir/Observation?code=4548-4", headers=tenant_headers)

    assert resp.status_code == 200
    body = resp.get_json()
    entries = body["entry"]
    modes = [e.get("search", {}).get("mode") for e in entries]
    assert "match" in modes
    assert "outcome" in modes  # upstream warnings survive the entry rebuild

    outcome_entry = next(e for e in entries
                         if e["resource"]["resourceType"] == "OperationOutcome")
    # the machine-readable code survives; the upstream free text, diagnostics,
    # extensions, and the nested object smuggled under `score` do not
    assert outcome_entry["resource"]["issue"][0]["code"] == "not-supported"
    blob = json.dumps(body)
    assert "ignored parameter foo" not in blob   # upstream text not forwarded
    assert "Rosa Hernandez" not in blob
    assert "db.internal" not in blob
    assert "extension" not in outcome_entry.get("search", {})
    assert "score" not in outcome_entry.get("search", {})  # nested value dropped


def test_medplum_not_found_read_is_audited(client, tenant_headers, tenant_id, app):
    proxy = _medplum_proxy_returning(None, 404)
    with patch("r6.routes.get_proxy_for_request", return_value=proxy):
        resp = client.get("/r6/fhir/Patient/gone", headers=tenant_headers)

    assert resp.status_code == 404
    # A not-found access attempt is still an access attempt — audited
    with app.app_context():
        rec = AuditEventRecord.query.filter_by(
            tenant_id=tenant_id, resource_type="Patient",
            event_type="read", outcome="failure").first()
        assert rec is not None
        assert "404" in rec.detail


def test_medplum_non_bundle_search_result_does_not_500(client, tenant_headers):
    """A 200 that is a bare resource instead of a Bundle must surface as a
    502 error, not crash the route or become a misleading empty searchset."""
    proxy = _medplum_proxy_returning({"resourceType": "Patient", "id": "p1"}, 200)
    with patch("r6.routes.get_proxy_for_request", return_value=proxy):
        resp = client.get("/r6/fhir/Observation?code=x", headers=tenant_headers)

    assert resp.status_code == 502
    body = resp.get_json()
    assert body["resourceType"] == "OperationOutcome"
