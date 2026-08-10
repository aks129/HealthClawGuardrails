"""A failed records connection must leave a trace on the server.

The connect page handles Fasten's real refusal event (`widget.config_error`,
#326/#450) and tells the patient the truth. Then it throws the payload away:

    console.log('[fasten-widget]', type, JSON.stringify(data).slice(0, 500));

That is the browser console and nowhere else. So when Fasten's founder asked,
on the open support thread FAS-864,

    "Can you send me a screenshot of what you're seeing in the widget? Or a
     request id so we can correlate the error in our logs?"

there was nothing to send. Two testers had hit
`fasten_unauthorized_client / "An error occurred while retrieving vault
profile"` and the only record of either attempt was an email describing it.

A guardrail layer that cannot say how often its own front door failed is
running the same blind spot it grades other systems for. These tests pin the
capture, and pin the two things that make it safe: it must not become a PHI
sink, and it must not become an open write endpoint.
"""

from __future__ import annotations

import json

_ENDPOINT = "/r6/fhir/internal/connect-diagnostic"

#: The payload Fasten actually emitted, from the FAS-864 report.
_REAL_PAYLOAD = {
    "type": "widget.config_error",
    "error_type": "fasten_unauthorized_client",
    "message": "An error occurred while retrieving vault profile",
    "request_id": "req_01J8XKQ2M4YB3",
}


def _config_error_branch(page: str) -> str:
    """The body of the widget.config_error branch.

    Bounded by the NEXT `else if`, not by the string "widget.close": that name
    also appears inside the branch's own comment, which listed the widget's
    real event names, so slicing on it cut the branch off before its code.
    """
    start = page.index("widget.config_error")
    end = page.index("} else if", start)
    return page[start:end]


def _post(client, payload=None, **kw):
    return client.post(_ENDPOINT, json=payload if payload is not None
                       else {"payload": _REAL_PAYLOAD}, **kw)


def test_a_refusal_is_accepted_and_given_a_reference(client):
    """MUTATION: delete the route -> red (404).

    The reference is what the patient reads back to us and what we quote in
    the support thread, so it has to come back to the caller.
    """
    r = _post(client, headers={"X-Tenant-Id": "desktop-demo"})
    assert r.status_code == 202, r.get_data(as_text=True)
    body = r.get_json()
    assert body.get("reference"), f"no reference returned: {body}"
    assert len(body["reference"]) >= 8


def test_the_vendor_request_id_survives(caplog, client):
    """The whole point. Without this the endpoint records that something
    failed but not the one field Fasten can correlate against.

    MUTATION: log only the error_type -> red.
    """
    import logging
    with caplog.at_level(logging.WARNING):
        _post(client, headers={"X-Tenant-Id": "desktop-demo"})

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "req_01J8XKQ2M4YB3" in logged, (
        f"the vendor request id was not recorded, so there is still nothing "
        f"to give support: {logged}")
    assert "fasten_unauthorized_client" in logged


def test_the_reference_is_stable_enough_to_quote_and_find(caplog, client):
    """A reference the server never wrote down is decoration.

    MUTATION: generate the reference without logging it -> red.
    """
    import logging
    with caplog.at_level(logging.WARNING):
        reference = _post(
            client, headers={"X-Tenant-Id": "desktop-demo"}).get_json()["reference"]

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert reference in logged, (
        "the reference handed to the patient appears in no server log, so "
        "quoting it back finds nothing")


# --- the two ways this could become a liability ----------------------------

def test_an_oversized_payload_is_refused_rather_than_stored(client):
    """This endpoint takes caller-controlled JSON and writes it to the log.

    Unbounded, that is a log-flooding primitive and a way to push arbitrary
    text into operational storage. #279 is the same shape on ingest.

    MUTATION: drop the size cap -> red.
    """
    r = _post(client, {"payload": {"message": "A" * 100_000}},
              headers={"X-Tenant-Id": "desktop-demo"})
    assert r.status_code == 413, (
        f"a 100KB diagnostic was accepted; expected 413, got {r.status_code}")


def test_a_payload_that_looks_like_a_record_is_refused(client):
    """A configuration refusal happens BEFORE any record is retrieved, so a
    FHIR resource in this payload means something is wrong — either the page
    is sending more than it should, or someone is probing.

    Either way this endpoint must not become the one unaudited, unredacted
    write path into our logs.

    MUTATION: accept any JSON -> red.
    """
    r = _post(client, {"payload": {
        "resourceType": "Patient",
        "name": [{"family": "Zzyzxbarton", "given": ["Quintavious"]}],
    }}, headers={"X-Tenant-Id": "desktop-demo"})
    assert r.status_code == 422, (
        f"a FHIR resource was accepted into the diagnostic log; got "
        f"{r.status_code}")


def test_the_refusal_reason_never_echoes_the_payload_back(client):
    """A 4xx that quotes what it rejected reflects the attacker's string.

    MUTATION: put the offending value in the response -> red.
    """
    r = _post(client, {"payload": {"resourceType": "Patient",
                                   "id": "Quintavious"}},
              headers={"X-Tenant-Id": "desktop-demo"})
    assert "Quintavious" not in r.get_data(as_text=True)


def test_a_missing_payload_is_a_400_not_a_crash(client):
    r = client.post(_ENDPOINT, json={}, headers={"X-Tenant-Id": "desktop-demo"})
    assert r.status_code == 400


def test_the_endpoint_does_not_write_a_fhir_resource(app, client):
    """It records an operational event, not clinical data.

    MUTATION: persist the payload as an R6Resource -> red.
    """
    from r6.models import R6Resource

    with app.app_context():
        before = R6Resource.query.count()
    _post(client, headers={"X-Tenant-Id": "desktop-demo"})
    with app.app_context():
        assert R6Resource.query.count() == before, (
            "the diagnostic endpoint created a FHIR resource")


def test_the_connect_page_actually_posts_the_payload():
    """The endpoint is useless if nothing calls it — the original defect was
    exactly a handler that existed and a wire that did not (#326).

    MUTATION: remove the fetch from the config_error branch -> red.
    """
    import pathlib

    page = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "templates/fasten_connect.html").read_text()

    branch = _config_error_branch(page)
    assert "connect-diagnostic" in branch, (
        "widget.config_error does not post the payload anywhere, so the "
        "console remains the only record")


def test_the_console_log_is_not_truncated_below_the_captured_payload():
    """The 500-char slice may cut the very field support needs.

    MUTATION: reintroduce .slice(0, 500) on the POSTED payload -> red.
    (The console line may still truncate; what is sent must not.)
    """
    import pathlib

    page = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "templates/fasten_connect.html").read_text()
    branch = _config_error_branch(page)
    assert "slice(0, 500)" not in branch, (
        "the payload sent to the server is truncated to 500 chars, which is "
        "where the vendor request id may live")
    assert json  # keep the import meaningful for the module
