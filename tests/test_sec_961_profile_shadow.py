"""Security review of #961 (FHI-52): the `_profile` shadow of a profile claim.

In FHIR JSON a primitive array such as `meta.profile` has a companion
`meta._profile` holding each claim's `id` and `extension`. #961 filters
`meta.profile` and leaves `meta._profile` alone, so an upstream (or a stored
nested resource) can still carry text beside the claim: an element `id`, an
extension `url`. The kept list is also re-sorted and de-duplicated, so any
surviving `_profile[i]` now describes a different claim than it did upstream.

The first group fails on 092af77 and pins the leak. The second group passes
and records the encoding attacks that were tried against `keep_profiles`.
Synthetic data only.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from r6.fhir_proxy import reset_proxy
from r6.redaction import apply_redaction
from r6.us_core_profiles import keep_profiles

_UC = "http://hl7.org/fhir/us/core/StructureDefinition/"
_PATIENT = _UC + "us-core-patient"
_CANARY = "Jane-Doe-1970"


def _shadow():
    return [{"id": _CANARY,
             "extension": [{"url": f"https://ehr.example/{_CANARY}/ext"}]}]


def _upstream_patient():
    return {
        "resourceType": "Patient", "id": "up-p-1",
        "meta": {"profile": [f"https://ehr.example/{_CANARY}",
                             _PATIENT + "|7.0.0"],
                 "_profile": _shadow()},
        "contained": [{"resourceType": "Coverage", "id": "c1",
                       "meta": {"profile": [_PATIENT],
                                "_profile": _shadow()}}],
    }


def _shadows(obj):
    """Every `meta._profile` at any depth."""
    found = []
    if isinstance(obj, list):
        for item in obj:
            found += _shadows(item)
    elif isinstance(obj, dict):
        meta = obj.get("meta")
        if isinstance(meta, dict) and "_profile" in meta:
            found.append(meta["_profile"])
        for value in obj.values():
            found += _shadows(value)
    return found


# --- fails on 092af77: the shadow survives ------------------------------------

def test_redaction_drops_the_profile_shadow():
    out = apply_redaction(_upstream_patient())
    assert _CANARY not in json.dumps(out)
    assert _shadows(out) == []


class TestUpstreamShadow:

    @pytest.fixture(autouse=True)
    def setup(self, app, client, tenant_headers):
        self.client = client
        self.tenant_headers = tenant_headers
        reset_proxy()
        os.environ["FHIR_UPSTREAM_URL"] = "https://hapi.fhir.org/baseR4"
        yield
        reset_proxy()
        os.environ.pop("FHIR_UPSTREAM_URL", None)

    def _get(self, path, method, value):
        with patch("r6.routes.get_proxy_for_request") as mock_get:
            proxy = MagicMock()
            getattr(proxy, method).return_value = (value, 200)
            mock_get.return_value = proxy
            return self.client.get(path, headers=self.tenant_headers)

    def test_upstream_read(self):
        resp = self._get("/r6/fhir/Patient/up-p-1", "read",
                         _upstream_patient())
        assert resp.status_code == 200
        assert _CANARY not in resp.get_data(as_text=True)

    def test_upstream_search(self):
        bundle = {"resourceType": "Bundle", "type": "searchset", "total": 1,
                  "entry": [{"resource": _upstream_patient()}]}
        resp = self._get("/r6/fhir/Patient", "search", bundle)
        assert resp.status_code == 200
        assert _CANARY not in resp.get_data(as_text=True)


def test_a_stored_nested_shadow_never_reaches_read_or_share(
        app, client, tenant_headers, auth_headers):
    from models import db
    from r6.models import R6Resource
    resource = _upstream_patient()
    resource["id"] = "sh-p-1"
    db.session.add(R6Resource(
        resource_type="Patient", resource_json=json.dumps(resource),
        resource_id="sh-p-1", tenant_id="test-tenant"))
    db.session.commit()
    resps = [client.get("/r6/fhir/Patient/sh-p-1", headers=tenant_headers),
             client.get("/r6/fhir/Patient/sh-p-1/$deidentify",
                        headers=tenant_headers)]
    resps += [client.post("/r6/fhir/$share-bundle",
                          json={"resource_types": ["Patient"],
                                "profile": profile},
                          headers=auth_headers)
              for profile in ("intake", "deidentified")]
    for resp in resps:
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert _CANARY not in resp.get_data(as_text=True)


# --- passes: encoding attacks on keep_profiles ---------------------------------

@pytest.mark.parametrize("claim", [
    _PATIENT.replace("-patient", "%2Dpatient"),
    _PATIENT + "%7C7.0.0",
    _PATIENT.replace("core", "сore"),       # Cyrillic es
    _PATIENT.replace("http:", "http："),     # fullwidth colon
    _PATIENT + "‍",                         # zero-width joiner
    _PATIENT + "\x00",
    _PATIENT + "x" * 100_000,
])
def test_an_encoded_near_miss_is_dropped(claim):
    assert keep_profiles([claim]) == []


@pytest.mark.parametrize("claim", [
    _PATIENT + "|",
    _PATIENT + "||",
    _PATIENT + "|7.0.0|",
    _PATIENT + "| 7.0.0",
    _PATIENT + "|7.0.0\n",
    _PATIENT + "|" + "7" * 100_000,
])
def test_a_malformed_version_is_reduced_to_the_bare_url(claim):
    assert keep_profiles([claim]) == [_PATIENT]
