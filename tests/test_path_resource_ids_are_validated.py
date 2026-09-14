"""A resource rule's path id is validated before any handler runs (#279, #281).

`/<resource_type>/<resource_id>` accepted anything: a 402-character sentence
was answered 404 with the sentence echoed in the diagnostics, and in proxy
mode the not-found audit row copied it into a `String(255)` column that
Postgres refuses past its width. The body-side id patterns existed; the
path had none.
"""
import json

HOSTILE = ("Josephine Rivera lives at 12 Elm Street and her SSN is "
           "123-45-6789 ") * 6
LONG_BUT_VALID = "fasten-" + "a" * 200  # the live connector's ids pass 64


def _confirmed(headers):
    # X-Human-Confirmed is the known gap (#214), used only to reach the
    # existing direct-write route; no new write path is built on it.
    return {**headers, "X-Human-Confirmed": "true"}


def test_a_hostile_path_id_is_refused_without_being_echoed(client,
                                                            tenant_headers):
    resp = client.get(f"/r6/fhir/Patient/{HOSTILE}", headers=tenant_headers)
    text = resp.get_data(as_text=True)
    assert resp.status_code == 400, text
    assert resp.get_json()["issue"][0]["code"] == "invalid"
    assert "Elm Street" not in text
    assert "Josephine" not in text


def test_the_update_route_refuses_it_before_the_write_gate(client,
                                                            auth_headers):
    resp = client.put(f"/r6/fhir/Patient/{HOSTILE}",
                      data=json.dumps({"resourceType": "Patient",
                                       "gender": "other"}),
                      content_type="application/json",
                      headers=_confirmed(auth_headers))
    text = resp.get_data(as_text=True)
    assert resp.status_code == 400, text
    assert "Elm Street" not in text


def test_the_tenant_error_still_comes_first(client):
    resp = client.get(f"/r6/fhir/Patient/{HOSTILE}",
                      headers={"X-Tenant-Id": "not a tenant!"})
    assert resp.status_code == 400
    assert "X-Tenant-Id" in resp.get_json()["issue"][0]["diagnostics"]


def test_an_ingested_id_longer_than_fhir_allows_still_reads(client,
                                                            auth_headers,
                                                            tenant_headers):
    """The width is 255, not 64: the live connector exports longer ids and
    the store was widened for them. A read must not re-impose the ceiling."""
    resp = client.post("/r6/fhir/Patient",
                       data=json.dumps({"resourceType": "Patient",
                                        "id": LONG_BUT_VALID,
                                        "gender": "other"}),
                       content_type="application/json",
                       headers=_confirmed(auth_headers))
    # Create validates a client id at 64 (its own rule); reading is the path
    # under test, so store the row the way the ingester would.
    if resp.status_code != 201:
        from r6.models import R6Resource, db
        with client.application.app_context():
            db.session.add(R6Resource(
                "Patient",
                json.dumps({"resourceType": "Patient", "id": LONG_BUT_VALID,
                            "gender": "other"}),
                resource_id=LONG_BUT_VALID,
                tenant_id=tenant_headers["X-Tenant-Id"]))
            db.session.commit()
    resp = client.get(f"/r6/fhir/Patient/{LONG_BUT_VALID}",
                      headers=tenant_headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)


def test_a_well_formed_missing_id_is_still_a_404(client, tenant_headers):
    resp = client.get("/r6/fhir/Patient/no-such-id.1", headers=tenant_headers)
    assert resp.status_code == 404
