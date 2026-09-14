"""`_id` is a declared search parameter with a real filter.

The conformance probe searches `GET /Patient?_id=<pid>` and asserts the
searchset contains that patient. `_id` was never in the registry, so locally
the check passed only because an unknown parameter is ignored and every
patient came back; in proxy mode it was forwarded and honoured upstream until
#730 started forwarding only declared parameters. Declaring it, with a filter
on the row itself, makes the probe prove what it says on both paths.
"""
import json
from unittest.mock import MagicMock, patch


def _seed(client, auth_headers, pid):
    resp = client.post("/r6/fhir/Patient",
                       data=json.dumps({"resourceType": "Patient", "id": pid,
                                        "gender": "other"}),
                       content_type="application/json",
                       headers={**auth_headers, "X-Human-Confirmed": "true"})
    assert resp.status_code == 201, resp.get_data(as_text=True)


def test_id_search_returns_only_the_row_asked_for(client, auth_headers,
                                                  tenant_headers):
    _seed(client, auth_headers, "id-search-a")
    _seed(client, auth_headers, "id-search-b")
    resp = client.get("/r6/fhir/Patient?_id=id-search-b", headers=tenant_headers)
    assert resp.status_code == 200
    bundle = resp.get_json()
    ids = [e["resource"]["id"] for e in bundle.get("entry", [])
           if e.get("search", {}).get("mode") != "outcome"]
    assert ids == ["id-search-b"]
    assert bundle["total"] == 1
    # Declared: no "unknown parameter" warning rides along, and the self
    # link keeps the parameter.
    assert not [e for e in bundle.get("entry", [])
                if e.get("search", {}).get("mode") == "outcome"]
    assert "_id=id-search-b" in bundle["link"][0]["url"]


def test_id_is_forwarded_upstream_in_proxy_mode(client, tenant_headers):
    proxy = MagicMock()
    proxy.search.return_value = ({"resourceType": "Bundle", "type": "searchset",
                                  "total": 0, "entry": []}, 200)
    with patch("r6.routes.get_proxy_for_request", return_value=proxy):
        resp = client.get("/r6/fhir/Patient?_id=up-1", headers=tenant_headers)
    assert resp.status_code == 200
    proxy.search.assert_called_once_with("Patient", {"_id": "up-1"})


def test_metadata_declares_id(client):
    resp = client.get("/r6/fhir/metadata")
    assert resp.status_code == 200
    patient = next(r for r in resp.get_json()["rest"][0]["resource"]
                   if r["type"] == "Patient")
    assert "_id" in [p["name"] for p in patient["searchParam"]]
