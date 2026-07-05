"""The self-conformance endpoint runs the harness against the live app.

GET /r6/fhir/$conformance probes the running deployment in-process and returns
the scorecard — a one-URL "prove the guardrails hold" for partners and demos.
"""

import json


def test_conformance_endpoint_grades_the_live_app(client):
    r = client.get("/r6/fhir/$conformance")
    assert r.status_code == 200
    body = r.get_json()
    assert body["passed"] is True
    assert body["grade"] == "A"
    assert body["score"]["passed"] == body["score"]["total"] == 6
    keys = {p["key"] for p in body["properties"]}
    assert keys == {"phi_redaction", "audit_trail", "step_up_enforcement",
                    "human_in_the_loop", "tenant_isolation", "medical_disclaimer"}


def test_conformance_endpoint_text_format(client):
    r = client.get("/r6/fhir/$conformance?format=text")
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    assert "Grade: A" in text and "PHI Redaction" in text


def test_conformance_uses_isolated_selftest_tenant(client):
    # The self-test writes synthetic data to a dedicated tenant, never a caller's.
    r = client.get("/r6/fhir/$conformance")
    assert r.get_json()["tenant"] == "conformance-selftest"
