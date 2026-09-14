"""A create or update response is an access too (#380).

For a long time POST and PUT echoed the caller's resource back with the
upstream `display` intact while a read of the same row was redacted and
re-labelled from r6/terminology.py. Nothing new was disclosed, since the
caller had just sent that text; but the property "every FHIR resource access
is redacted" carries no exception, and the conformance badge said A while
never looking at a 201 body. `probe_phi_redaction` now covers the create
response; this file pins the update response, which the probe does not
exercise, and the read that follows both.
"""
import json

JUNK = "UPSTREAM-DISPLAY-MUST-NOT-SURVIVE"
LABEL = "Cholesterol (total)"  # LOINC 2093-3 in the static terminology table


def _obs(value):
    return {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "2093-3",
                             "display": JUNK}],
                 "text": JUNK},
        "valueQuantity": {"value": value, "unit": "mg/dL"},
    }


def _write(client, method, path, body, headers):
    # X-Human-Confirmed is the known gap (#214), used here only to reach the
    # existing direct-write route the way the conformance probe does; no new
    # write path is built on it.
    resp = getattr(client, method)(path, data=json.dumps(body),
                                   content_type="application/json",
                                   headers={**headers, "X-Human-Confirmed": "true"})
    return resp, resp.get_data(as_text=True)


def test_the_update_response_is_re_labelled_not_echoed(client, auth_headers):
    resp, text = _write(client, "post", "/r6/fhir/Observation", _obs(188),
                        auth_headers)
    assert resp.status_code == 201, text
    oid = resp.get_json()["id"]

    updated = dict(_obs(190), id=oid)
    resp, text = _write(client, "put", f"/r6/fhir/Observation/{oid}", updated,
                        {**auth_headers, "If-Match": resp.headers["ETag"]})
    assert resp.status_code == 200, text
    assert JUNK not in text, "the PUT response echoed the upstream display"
    assert LABEL in text, "the PUT response was not re-labelled from our table"


def test_the_create_response_is_re_labelled_not_echoed(client, auth_headers):
    """The conformance probe measures this too; kept here so the unit suite
    names the route when it regresses, rather than only the grade."""
    resp, text = _write(client, "post", "/r6/fhir/Observation", _obs(188),
                        auth_headers)
    assert resp.status_code == 201, text
    assert JUNK not in text
    assert LABEL in text
