"""Exploit cases for `meta.profile` (FHI-52).

Before FHI-52, redaction kept any `meta.profile` value. Local reads hid that
because `to_fhir_json` rebuilt `meta`; upstream proxy reads did not, so a
profile URL carrying a patient's name reached the caller. These tests plant
hostile claims upstream, in stored JSON, and in nested resources, and hold
that only an allow-listed canonical ever comes out. Synthetic data only.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from r6.fhir_proxy import reset_proxy
from r6.models import R6Resource
from r6.redaction import apply_patient_controlled_redaction, apply_redaction
from r6.us_core_profiles import keep_profiles

_UC = "http://hl7.org/fhir/us/core/StructureDefinition/"
_PATIENT = _UC + "us-core-patient"
_CANARY = "Jane-Doe-1970"
_NAME_URL = f"https://ehr.example/patients/{_CANARY}/profile"


def _hostile_profile():
    """Everything an upstream can put in `meta.profile`, beside one claim
    that is allowed."""
    return [_NAME_URL, {"url": _NAME_URL}, 42, None, [_NAME_URL],
            _PATIENT + "|7.0.0"]


@pytest.mark.parametrize("claim", [
    _PATIENT + "-evil",
    _PATIENT + "/../x",
    _PATIENT.upper(),
    "HTTP://hl7.org/fhir/us/core/StructureDefinition/us-core-patient",
    " " + _PATIENT,
    _PATIENT + " ",
    _PATIENT + "?name=" + _CANARY,
    _PATIENT + "#" + _CANARY,
    "https://hl7.org/fhir/us/core/StructureDefinition/us-core-patient",
    "http://evil.example/fhir/us/core/StructureDefinition/us-core-patient",
    _UC + "us-core-observation-lab",
])
def test_a_near_miss_is_dropped(claim):
    assert keep_profiles([claim]) == []


@pytest.mark.parametrize("claim", [
    _PATIENT + "|7.0.0 " + _CANARY,
    _PATIENT + "|" + _CANARY,
    _PATIENT + "|7.0.0|" + _CANARY,
])
def test_a_version_carrying_text_is_reduced_to_the_bare_url(claim):
    assert keep_profiles([claim]) == [_PATIENT]


@pytest.mark.parametrize("value", [
    _PATIENT, _NAME_URL, {"0": _PATIENT}, 7, None, True,
])
def test_a_profile_that_is_not_a_list_is_dropped(value):
    """Even a valid canonical as a bare string: the field is a list."""
    assert keep_profiles(value) == []
    out = apply_redaction({"resourceType": "Patient",
                           "meta": {"profile": value}})
    assert "profile" not in out["meta"]


# --- the live leak: upstream reads --------------------------------------------

class TestUpstreamProxy:

    @pytest.fixture(autouse=True)
    def setup(self, app, client, tenant_headers):
        self.client = client
        self.tenant_headers = tenant_headers
        reset_proxy()
        os.environ["FHIR_UPSTREAM_URL"] = "https://hapi.fhir.org/baseR4"
        yield
        reset_proxy()
        os.environ.pop("FHIR_UPSTREAM_URL", None)

    @staticmethod
    def _patient():
        return {
            "resourceType": "Patient", "id": "up-p-1",
            "meta": {"profile": _hostile_profile()},
            "contained": [{"resourceType": "Coverage", "id": "c1",
                           "meta": {"profile": [_NAME_URL]}}],
        }

    def test_read_never_surfaces_a_hostile_profile(self):
        """MUTATION: drop the redaction-layer filter -> red."""
        with patch("r6.routes.get_proxy_for_request") as mock_get:
            proxy = MagicMock()
            proxy.read.return_value = (self._patient(), 200)
            mock_get.return_value = proxy
            resp = self.client.get("/r6/fhir/Patient/up-p-1",
                                   headers=self.tenant_headers)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_data(as_text=True)
        assert _CANARY not in body
        data = resp.get_json()
        assert data["meta"]["profile"] == [_PATIENT + "|7.0.0"]
        assert "meta" not in data["contained"][0] or \
            "profile" not in data["contained"][0]["meta"]

    def test_search_never_surfaces_a_hostile_profile(self):
        bundle = {"resourceType": "Bundle", "type": "searchset", "total": 1,
                  "entry": [{"resource": self._patient()}]}
        with patch("r6.routes.get_proxy_for_request") as mock_get:
            proxy = MagicMock()
            proxy.search.return_value = (bundle, 200)
            mock_get.return_value = proxy
            resp = self.client.get("/r6/fhir/Patient",
                                   headers=self.tenant_headers)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert _CANARY not in resp.get_data(as_text=True)
        resource = resp.get_json()["entry"][0]["resource"]
        assert resource["meta"]["profile"] == [_PATIENT + "|7.0.0"]


# --- every depth ---------------------------------------------------------------

def _nested():
    hostile = {"profile": _hostile_profile()}
    return {
        "resourceType": "Condition", "id": "n-c-1",
        "meta": json.loads(json.dumps(hostile)),
        "contained": [
            {"resourceType": "Observation", "id": "o1",
             "meta": json.loads(json.dumps(hostile))},
            {"resourceType": "Bundle", "id": "b1", "type": "collection",
             "entry": [{"resource": {
                 "resourceType": "Patient", "id": "p2",
                 "meta": json.loads(json.dumps(hostile))}}]},
        ],
    }


def _profiles(obj):
    """Every `meta.profile` value at any depth."""
    found = []
    if isinstance(obj, list):
        for item in obj:
            found += _profiles(item)
    elif isinstance(obj, dict):
        meta = obj.get("meta")
        if isinstance(meta, dict) and "profile" in meta:
            found.append(meta["profile"])
        for value in obj.values():
            found += _profiles(value)
    return found


@pytest.mark.parametrize("redact", [
    apply_redaction,
    lambda r: apply_patient_controlled_redaction(r, "n-p-1"),
])
def test_redaction_filters_every_depth(redact):
    out = redact(_nested())
    assert _CANARY not in json.dumps(out)
    assert _profiles(out) == [[_PATIENT + "|7.0.0"]] * 3
    # Run twice: the same result.
    assert _profiles(redact(out)) == _profiles(out)


def test_to_fhir_json_filters_every_depth(app):
    """Local exits that never redact (exports, labs, care gaps) see the same
    filtered claims, nested ones included."""
    row = R6Resource(resource_type="Condition",
                     resource_json=json.dumps(_nested()),
                     resource_id="n-c-1", tenant_id="test-tenant")
    out = row.to_fhir_json()
    assert _CANARY not in json.dumps(out)
    assert _profiles(out) == [[_PATIENT + "|7.0.0"]] * 3


def test_a_stored_hostile_profile_never_reaches_read_or_share(
        app, client, tenant_headers, auth_headers):
    """Planted through the ORM, so the read exits are tested, not the write
    path."""
    from models import db
    db.session.add(R6Resource(
        resource_type="Condition", resource_json=json.dumps(_nested()),
        resource_id="n-c-1", tenant_id="test-tenant"))
    db.session.commit()
    read = client.get("/r6/fhir/Condition/n-c-1", headers=tenant_headers)
    search = client.get("/r6/fhir/Condition", headers=tenant_headers)
    shares = [client.post("/r6/fhir/$share-bundle",
                          json={"resource_types": ["Condition"],
                                "profile": profile},
                          headers=auth_headers)
              for profile in ("intake", "deidentified")]
    for resp in [read, search, *shares]:
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert _CANARY not in resp.get_data(as_text=True)
        assert _profiles(resp.get_json()) == [[_PATIENT + "|7.0.0"]] * 3


def _underscored(obj):
    """Every `_`-prefixed key inside any `meta`, at any depth."""
    found = []
    if isinstance(obj, list):
        for item in obj:
            found += _underscored(item)
    elif isinstance(obj, dict):
        meta = obj.get("meta")
        if isinstance(meta, dict):
            found += [k for k in meta if k.startswith("_")]
        for value in obj.values():
            found += _underscored(value)
    return found


def test_no_meta_companion_element_survives(app):
    """`meta._profile` and every other `_` companion in `meta` carry
    upstream `id`/`extension` text. We rebuild none, so all are dropped,
    whether or not that meta has a `profile` (#961 security review).

    MUTATION: drop the companion pop in `filter_meta` -> red."""
    shadow = [{"id": _CANARY, "extension": [{"url": _NAME_URL}]}]
    meta = {"profile": [_PATIENT], "_profile": shadow,
            "_versionId": {"id": _CANARY}, "_lastUpdated": {"id": _CANARY},
            "_source": {"extension": [{"url": _NAME_URL}]}}
    no_profile = {"_profile": shadow, "_source": {"id": _CANARY}}
    resource = {"resourceType": "Patient", "id": "u-p-1",
                "meta": json.loads(json.dumps(meta)),
                "contained": [
                    {"resourceType": "Coverage", "id": "c1",
                     "meta": json.loads(json.dumps(meta))},
                    {"resourceType": "Coverage", "id": "c2",
                     "meta": json.loads(json.dumps(no_profile))}]}
    row = R6Resource(resource_type="Patient",
                     resource_json=json.dumps(resource),
                     resource_id="u-p-1", tenant_id="test-tenant")
    for out in (apply_redaction(resource),
                apply_patient_controlled_redaction(resource, "u-p-1"),
                row.to_fhir_json()):
        assert _underscored(out) == []
        assert _CANARY not in json.dumps(out)
        assert out["meta"]["profile"] == [_PATIENT]
