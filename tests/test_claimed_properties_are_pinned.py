"""Pins for properties this project claims but did not test.

Found by a mutation audit: 48 mutations, 8 survivors. Each test below has a
`MUTATION:` line naming the exact edit that must turn it red. If you change
the code these guard, verify the named mutation still fails — a pin that
cannot fail is the thing this file exists to stop.

The properties here are load-bearing in the README, the conformance badge and
the webinar deck. Two of them were previously demonstrated only on a path no
user takes.
"""

from __future__ import annotations

import json
import time

import pytest

TENANT = "test-tenant"

_FAMILY = "Secretsurname"
_GIVEN = "Confidentialgiven"
_SSN = "123-45-6789"
_PHONE = "617-555-0142"
_STREET = "42 Realstreet Ave"
_DOB = "1980-03-14"


def _patient_with_phi(pid: str) -> dict:
    return {
        "resourceType": "Patient",
        "id": pid,
        "name": [{"family": _FAMILY, "given": [_GIVEN],
                  "text": f"{_GIVEN} Q. {_FAMILY}"}],
        "identifier": [{"system": "http://hl7.org/fhir/sid/us-ssn",
                        "value": _SSN}],
        "telecom": [{"system": "phone", "value": _PHONE}],
        "address": [{"line": [_STREET], "city": "Boston"}],
        "birthDate": _DOB,
    }


def _seed(app, pid: str) -> None:
    from models import db
    from r6.models import R6Resource
    with app.app_context():
        db.session.add(R6Resource(
            tenant_id=TENANT, resource_type="Patient", resource_id=pid,
            resource_json=json.dumps(_patient_with_phi(pid))))
        db.session.commit()


_MARKERS = [("family name", _FAMILY), ("given name", _GIVEN),
            ("SSN", _SSN), ("phone", _PHONE), ("street", _STREET),
            ("date of birth", _DOB)]


# --- S1: redaction on the SEARCH path --------------------------------------

def test_search_redacts_phi(client, app):
    """MUTATION: drop `apply_redaction` from the search loop in r6/routes.py.

    Read-by-id was well covered. Search was not — and search is what builds an
    agent's context (`careagents/healthclaw.py` search()). With redaction
    removed from this loop the entire suite stayed green and the conformance
    grade stayed A, so the badge attested to a path no model reads through.
    """
    _seed(app, "phi-search-probe")
    r = client.get("/r6/fhir/Patient?_id=phi-search-probe",
                   headers={"X-Tenant-Id": TENANT})
    assert r.status_code == 200
    blob = r.get_data(as_text=True)
    leaked = [label for label, value in _MARKERS if value in blob]
    assert not leaked, f"search leaked {', '.join(leaked)}"


def test_search_redacts_phi_even_without_an_id_filter(client, app):
    """The unfiltered listing is the shape an agent actually issues."""
    _seed(app, "phi-search-probe-2")
    r = client.get("/r6/fhir/Patient?_count=20",
                   headers={"X-Tenant-Id": TENANT})
    assert r.status_code == 200
    blob = r.get_data(as_text=True)
    leaked = [label for label, value in _MARKERS if value in blob]
    assert not leaked, f"search listing leaked {', '.join(leaked)}"


def test_read_and_search_redact_the_same_fields(client, app):
    """Neither path may be the lenient one."""
    _seed(app, "phi-parity-probe")
    h = {"X-Tenant-Id": TENANT}
    read = client.get("/r6/fhir/Patient/phi-parity-probe",
                      headers=h).get_data(as_text=True)
    search = client.get("/r6/fhir/Patient?_id=phi-parity-probe",
                        headers=h).get_data(as_text=True)
    for label, value in _MARKERS:
        assert (value in read) == (value in search), (
            f"{label} is treated differently by read and search")


def test_human_name_text_never_survives(client, app):
    """MUTATION: stop stripping HumanName.text in r6/redaction.py.

    CLAUDE.md calls this out by name: real feeds put the full patient name in
    `name[].text`. `Coding.display` and `CodeableConcept.text` were pinned;
    this sibling field was not.
    """
    _seed(app, "phi-nametext-probe")
    for path in ("/r6/fhir/Patient/phi-nametext-probe",
                 "/r6/fhir/Patient?_id=phi-nametext-probe"):
        blob = client.get(path, headers={"X-Tenant-Id": TENANT}
                          ).get_data(as_text=True)
        assert f"{_GIVEN} Q. {_FAMILY}" not in blob, f"name text survived {path}"


# --- S4: step-up token expiry ----------------------------------------------

def test_an_expired_step_up_token_is_refused():
    """MUTATION: delete the `exp` check in r6/stepup.py validate_step_up_token.

    Signature, tenant binding, scope and replay were all pinned. The TTL was
    not, so `DEFAULT_TOKEN_TTL_SECONDS = 300` was enforced by code no test
    exercised. A token from a screenshot or an old log would authorize writes
    forever.
    """
    from r6.stepup import generate_step_up_token, validate_step_up_token

    token = generate_step_up_token(TENANT, ttl_seconds=-1)
    ok, reason = validate_step_up_token(token, TENANT)
    assert ok is False, "an expired token was accepted"
    assert "expired" in reason.lower(), reason


def test_a_live_step_up_token_is_still_accepted():
    """Otherwise the expiry check could be a blanket refusal."""
    from r6.stepup import generate_step_up_token, validate_step_up_token

    ok, _ = validate_step_up_token(
        generate_step_up_token(TENANT, ttl_seconds=300), TENANT)
    assert ok is True


def test_the_default_ttl_is_short():
    """A silent bump to days would pass every other test in the suite."""
    from r6.stepup import DEFAULT_TOKEN_TTL_SECONDS
    assert 0 < DEFAULT_TOKEN_TTL_SECONDS <= 900, (
        f"default step-up TTL is {DEFAULT_TOKEN_TTL_SECONDS}s; a step-up "
        "credential is meant to be minutes, not hours")


def test_expiry_is_enforced_at_the_boundary(monkeypatch):
    """Pins the direction of the comparison, not just that one exists."""
    from r6.stepup import generate_step_up_token, validate_step_up_token

    token = generate_step_up_token(TENANT, ttl_seconds=60)
    assert validate_step_up_token(token, TENANT)[0] is True

    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 61)
    ok, reason = validate_step_up_token(token, TENANT)
    assert ok is False and "expired" in reason.lower(), reason


# --- S2: audit on read -----------------------------------------------------

def test_a_read_writes_its_own_audit_event(client, app):
    """MUTATION: remove `record_audit_event` from local read_resource.

    The existing test asserted `total >= 1` after a POST that had already
    emitted a create event, so it passed with read auditing deleted. This
    filters to the read.
    """
    _seed(app, "audit-read-probe")
    client.get("/r6/fhir/Patient/audit-read-probe",
               headers={"X-Tenant-Id": TENANT})

    from r6.models import AuditEventRecord
    with app.app_context():
        reads = AuditEventRecord.query.filter_by(
            tenant_id=TENANT, resource_type="Patient",
            resource_id="audit-read-probe", event_type="read").all()
    assert reads, "reading a resource emitted no read AuditEvent"


@pytest.mark.parametrize("marker", [v for _, v in _MARKERS])
def test_the_audit_trail_carries_no_phi(client, app, marker):
    """The audit trail is shown to patients; it must not become the leak."""
    _seed(app, "audit-phi-probe")
    client.get("/r6/fhir/Patient/audit-phi-probe",
               headers={"X-Tenant-Id": TENANT})
    blob = client.get("/r6/fhir/AuditEvent?_count=100",
                      headers={"X-Tenant-Id": TENANT}).get_data(as_text=True)
    assert marker not in blob
