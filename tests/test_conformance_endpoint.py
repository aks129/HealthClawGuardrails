"""The self-conformance endpoint runs the harness against the live app.

GET /r6/fhir/$conformance probes the running deployment in-process and returns
the scorecard — a one-URL "prove the guardrails hold" for partners and demos.
"""



def test_conformance_endpoint_grades_the_live_app(client):
    r = client.get("/r6/fhir/$conformance")
    assert r.status_code == 200
    body = r.get_json()
    assert body["passed"] is True
    assert body["grade"] == "A"
    assert body["score"] == {"passed": 7, "total": 7}
    keys = {p["key"] for p in body["properties"]}
    assert keys == {"phi_redaction", "audit_trail", "step_up_enforcement",
                    "human_in_the_loop", "tenant_isolation", "medical_disclaimer",
                    "error_fidelity"}
    error_fidelity = next(
        p for p in body["properties"] if p["key"] == "error_fidelity")
    assert error_fidelity["grade"] == "A"
    assert error_fidelity["coverage"] == "local-fhir-only"
    assert error_fidelity["profiles"]["proxy"] == {
        "status": "not_run", "checks": []}


def test_conformance_endpoint_text_format(client):
    r = client.get("/r6/fhir/$conformance?format=text")
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    assert "Grade: A" in text and "PHI Redaction" in text
    assert "Error Fidelity — A (local-fhir-only)" in text
    assert "local: run — A" in text
    assert "mcp: not_run" in text
    assert "proxy: not_run" in text
    assert "PHI Redaction — A (full)" not in text


def test_conformance_uses_isolated_selftest_tenant(client):
    # The self-test writes synthetic data to a dedicated tenant, never a caller's.
    r = client.get("/r6/fhir/$conformance")
    assert r.get_json()["tenant"] == "conformance-selftest"


def test_conformance_shields_badge_format(client):
    r = client.get("/r6/fhir/$conformance?format=shields")
    assert r.status_code == 200
    b = r.get_json()
    assert b["schemaVersion"] == 1
    assert b["label"] == "guardrail conformance"
    assert b["message"] == "A (7/7; error fidelity A, local-fhir-only)"
    assert b["color"] == "brightgreen"
    assert set(b) == {"schemaVersion", "label", "message", "color"}


def test_conformance_report_states_its_own_scope(client):
    # #186: a Grade A must never read as a HIPAA assessment or third-party
    # audit — the report says so itself in every substantive output format.
    from r6.conformance.probes import SCOPE_STATEMENT

    assert "NOT a HIPAA Security Rule assessment" in SCOPE_STATEMENT
    assert "guardrail layer only" in SCOPE_STATEMENT

    body = client.get("/r6/fhir/$conformance").get_json()
    assert body["scope"] == SCOPE_STATEMENT

    text = client.get("/r6/fhir/$conformance?format=text").get_data(as_text=True)
    assert "SCOPE:" in text
    assert "NOT a HIPAA Security Rule assessment" in text
    # Scope sits between the grade and the property scorecard.
    assert text.index("Grade:") < text.index("SCOPE:") < text.index("[PASS]")


def test_conformance_is_cached_between_calls(client):
    # ?fresh=1 forces a new run (so badge/monitor traffic can reuse the cache
    # instead of re-running the harness — and its synthetic writes — each hit).
    forced = client.get("/r6/fhir/$conformance?fresh=1").get_json()
    assert forced["cached"] is False
    cached = client.get("/r6/fhir/$conformance").get_json()
    assert cached["cached"] is True
    again = client.get("/r6/fhir/$conformance?fresh=1").get_json()
    assert again["cached"] is False
