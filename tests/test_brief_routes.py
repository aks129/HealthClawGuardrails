"""Route tests for the AppointmentBrief endpoint.

#381: when the care-gaps evaluation raises, the brief must carry an explicit
"unavailable" state. The old behaviour returned {} and produced a care-gaps
section indistinguishable from a clean bill of health — a patient reading it
was told they were up to date on colorectal, cervical and breast cancer
screening by a component that never ran.

NOTE the URL. r6_blueprint is mounted at /r6/fhir and the handler adds a
second "/fhir", so the route registers at /r6/fhir/fhir/AppointmentBrief
while its only client (careagents/healthclaw.py) requests
/r6/fhir/AppointmentBrief. That mismatch is a separate defect, untouched
here; these tests exercise the path the app actually serves.
"""

from r6.brief.engine import (
    CARE_GAPS_OK,
    CARE_GAPS_REASON_ENGINE_ERROR,
    CARE_GAPS_UNAVAILABLE,
)

_URL = "/r6/fhir/fhir/AppointmentBrief"
_SECTION_PREFIX = "https://healthclaw.io/fhir/StructureDefinition/brief-section-"


def _section(body, name):
    for ext in body.get("extension", []):
        if ext.get("url") == _SECTION_PREFIX + name:
            return ext
    return None


def _sub(section, url):
    for e in section.get("extension", []):
        if e.get("url") == url:
            return e.get("valueString")
    return None


def _fields(section):
    return [e for e in section.get("extension", []) if e.get("url") == "field"]


def test_care_gaps_engine_failure_is_marked_unavailable(client, tenant_headers,
                                                        monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("screening rule table is corrupt")

    monkeypatch.setattr("r6.caregaps.evaluate.evaluate_care_gaps", _boom)

    r = client.get(_URL, headers=tenant_headers)
    assert r.status_code == 200  # a brief that 500s is worse than a named gap

    gaps = _section(r.get_json(), "care-gaps")
    assert gaps is not None
    assert _sub(gaps, "status") == CARE_GAPS_UNAVAILABLE
    assert _sub(gaps, "reason") == CARE_GAPS_REASON_ENGINE_ERROR
    # The failure is named, not silently rendered as an answered-and-empty
    # section: no consumer may read this as "no gaps".
    assert _sub(gaps, "status") != CARE_GAPS_OK
    assert _fields(gaps) == []


def test_care_gaps_section_always_carries_a_status(client, tenant_headers):
    """Every brief states whether the screening review ran.

    Without the marker a parser has only the field list to go on, which is
    the two-state shape #381 is about.
    """
    r = client.get(_URL, headers=tenant_headers)
    assert r.status_code == 200
    gaps = _section(r.get_json(), "care-gaps")
    assert _sub(gaps, "status") in (CARE_GAPS_OK, CARE_GAPS_UNAVAILABLE)


def test_other_sections_are_unchanged(client, tenant_headers):
    """The status marker is care-gaps only — the other four sections keep
    carrying nothing but fields."""
    r = client.get(_URL, headers=tenant_headers)
    body = r.get_json()
    for name in ("problems", "medications", "labs", "visits"):
        section = _section(body, name)
        assert section is not None
        assert all(e.get("url") == "field" for e in section["extension"])


def test_brief_requires_a_tenant(client):
    assert client.get(_URL).status_code == 400
