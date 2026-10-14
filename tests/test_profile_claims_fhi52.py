"""Profile claims a record carries out: `meta.profile` (FHI-52).

A SMART Health Check-in wallet matches a record to a request by its US Core
profile claim. Before this, `to_fhir_json` rebuilt `meta` wholesale, so local
reads lost every claim, while upstream reads kept any value at all.

What these tests hold:
  - the allow-list is closed: US Core check-in canonicals plus C4DIC-Coverage
    (owner ruling), each with the versions its IG published;
  - a listed claim survives read, search and both share-bundle profiles,
    versioned or bare; an unpublished version is reduced to the bare URL;
  - a record with no claim gets none (validate-then-claim is deferred);
  - the record-source tag still appears beside the claim, and `meta.source`
    and upstream tags still do not;
  - the filter gives the same result when run twice.
"""

from __future__ import annotations

import json

import pytest

from r6.models import RECORD_SOURCE_SYSTEM, R6Resource
from r6.redaction import apply_patient_controlled_redaction, apply_redaction
from r6.us_core_profiles import CANONICALS, VERSIONS, keep_profiles

_INGEST = "/r6/fhir/internal/ingest-bundle"
_UC = "http://hl7.org/fhir/us/core/StructureDefinition/"
_PATIENT = _UC + "us-core-patient"
_PROBLEM = _UC + "us-core-condition-problems-health-concerns"
_LEGACY_CONDITION = _UC + "us-core-condition"
_C4DIC = ("http://hl7.org/fhir/us/insurance-card/StructureDefinition/"
          "C4DIC-Coverage")
_ALL = {"3.1.1", "4.0.0", "5.0.1", "6.1.0", "7.0.0", "8.0.0"}


def _upload(client, resources, tenant="test-tenant"):
    body = {"bundle": {"resourceType": "Bundle", "type": "collection",
                       "entry": [{"resource": r} for r in resources]}}
    r = client.post(_INGEST, data=json.dumps(body),
                    headers={"X-Tenant-Id": tenant,
                             "Content-Type": "application/json"})
    assert r.status_code == 200, r.get_data(as_text=True)


def _condition(cid="pc-c-1", profile=None, meta=None):
    resource = {
        "resourceType": "Condition", "id": cid,
        "subject": {"reference": "Patient/pc-p-1"},
        "code": {"coding": [{"system": "http://snomed.info/sct",
                             "code": "38341003"}]},
    }
    if meta is not None:
        resource["meta"] = meta
    if profile is not None:
        resource.setdefault("meta", {})["profile"] = profile
    return resource


# --- the closed list ----------------------------------------------------------

def test_the_allow_list_is_closed():
    """Pinned row by row: growing it is a reviewed change with a test."""
    assert CANONICALS == {
        _PATIENT: _ALL,
        _LEGACY_CONDITION: {"3.1.1", "4.0.0"},
        _PROBLEM: {"5.0.1", "6.1.0", "7.0.0", "8.0.0"},
        _UC + "us-core-condition-encounter-diagnosis":
            {"5.0.1", "6.1.0", "7.0.0", "8.0.0"},
        _UC + "us-core-allergyintolerance": _ALL,
        _UC + "us-core-medicationrequest": _ALL,
        _UC + "us-core-immunization": _ALL,
        _UC + "us-core-coverage": {"6.1.0", "7.0.0", "8.0.0"},
        _UC + "us-core-organization": _ALL,
        _UC + "us-core-practitioner": _ALL,
        _C4DIC: {"1.0.0", "1.1.0"},
    }
    assert VERSIONS == {
        "us-core": {"3.1.1", "4.0.0", "5.0.1", "6.1.0", "7.0.0", "8.0.0"},
        "c4dic": {"1.0.0", "1.1.0"},
    }


@pytest.mark.parametrize("claim", [
    _PATIENT, _PATIENT + "|7.0.0", _PATIENT + "|3.1.1",
    _C4DIC, _C4DIC + "|1.1.0",
    _LEGACY_CONDITION, _LEGACY_CONDITION + "|3.1.1",
    _LEGACY_CONDITION + "|4.0.0", _PROBLEM + "|5.0.1",
])
def test_a_listed_claim_is_kept_as_written(claim):
    assert keep_profiles([claim]) == [claim]


@pytest.mark.parametrize("claim", [
    _PATIENT + "|9.9.9",       # never published
    _PATIENT + "|1.1.0",       # a C4DIC version, not a US Core one
    _C4DIC + "|7.0.0",         # a US Core version, not a C4DIC one
    _PATIENT + "|",            # empty version
    # A canonical paired with a version that did not publish it.
    _LEGACY_CONDITION + "|5.0.1",
    _LEGACY_CONDITION + "|7.0.0",
    _PROBLEM + "|4.0.0",
    _UC + "us-core-coverage|3.1.1",
])
def test_an_unpublished_version_is_reduced_to_the_bare_url(claim):
    assert keep_profiles([claim]) == [claim.split("|")[0]]


def test_output_is_deduplicated_and_sorted():
    got = keep_profiles([_PROBLEM, _PATIENT, _PATIENT, _PATIENT + "|9.9.9"])
    assert got == sorted({_PATIENT, _PROBLEM})


def test_keep_profiles_is_idempotent():
    corpus = [_PATIENT + "|7.0.0", _PATIENT + "|9.9.9", _C4DIC, 7, None,
              {"url": _PATIENT}, "https://ehr.example/x", _PROBLEM]
    once = keep_profiles(corpus)
    assert keep_profiles(once) == once


# --- local exits: to_fhir_json ------------------------------------------------

def test_to_fhir_json_keeps_a_listed_claim_beside_the_record_source_tag(app):
    """MUTATION: drop the `to_fhir_json` layer -> red (the claim goes
    missing, because the meta rebuild drops it)."""
    meta = {"profile": [_PROBLEM + "|7.0.0", "https://ehr.example/other"],
            "source": "https://ehr.example/feeds/1",
            "tag": [{"system": "http://upstream.example/t", "code": "x"}]}
    row = R6Resource(resource_type="Condition",
                     resource_json=json.dumps(_condition(meta=meta)),
                     resource_id="pc-c-1", tenant_id="test-tenant",
                     source="upload")
    out = row.to_fhir_json()
    assert out["meta"]["profile"] == [_PROBLEM + "|7.0.0"]
    assert out["meta"]["tag"] == [{"system": RECORD_SOURCE_SYSTEM,
                                   "code": "upload"}]
    assert "source" not in out["meta"]
    assert "ehr.example" not in json.dumps(out)


def test_an_unprofiled_record_stays_unprofiled(app, client, tenant_headers):
    """We stamp no claims: no claim in, no claim out, on every exit."""
    _upload(client, [_condition()])
    read = client.get("/r6/fhir/Condition/pc-c-1", headers=tenant_headers)
    assert read.status_code == 200
    assert "profile" not in read.get_json()["meta"]
    row = R6Resource.query.filter_by(tenant_id="test-tenant",
                                     id="pc-c-1").one()
    assert "profile" not in row.to_fhir_json()["meta"]
    assert "profile" not in apply_redaction(row.to_fhir_json())["meta"]


def test_a_claim_with_only_off_list_values_leaves_no_profile_key(app):
    row = R6Resource(resource_type="Condition",
                     resource_json=json.dumps(_condition(
                         profile=["https://ehr.example/p"])),
                     resource_id="pc-c-1", tenant_id="test-tenant")
    assert "profile" not in row.to_fhir_json()["meta"]


# --- HTTP: read, search, share-bundle -----------------------------------------

@pytest.mark.parametrize("claim", [_PROBLEM, _PROBLEM + "|6.1.0"])
def test_read_and_search_carry_the_claim(client, tenant_headers, claim):
    _upload(client, [_condition(profile=[claim])])
    read = client.get("/r6/fhir/Condition/pc-c-1", headers=tenant_headers)
    assert read.status_code == 200
    assert read.get_json()["meta"]["profile"] == [claim]
    search = client.get("/r6/fhir/Condition", headers=tenant_headers)
    assert search.status_code == 200
    assert [e["resource"]["meta"]["profile"]
            for e in search.get_json()["entry"]] == [[claim]]


@pytest.mark.parametrize("profile", ["intake", "deidentified"])
def test_the_share_bundle_carries_the_filtered_claim(client, auth_headers,
                                                     profile):
    _upload(client, [_condition(profile=[
        _PROBLEM + "|7.0.0", _PROBLEM + "|9.9.9",
        "https://ehr.example/patients/someone/profile"])])
    share = client.post("/r6/fhir/$share-bundle",
                        json={"resource_types": ["Condition"],
                              "profile": profile},
                        headers=auth_headers)
    assert share.status_code == 200, share.get_data(as_text=True)
    meta = share.get_json()["entry"][0]["resource"]["meta"]
    assert meta["profile"] == [_PROBLEM, _PROBLEM + "|7.0.0"]
    assert {"system": RECORD_SOURCE_SYSTEM, "code": "upload"} in meta["tag"]
    assert "ehr.example" not in share.get_data(as_text=True)


# --- the check-in test patient (#959) -----------------------------------------

# The claims the synthetic check-in patient (#959,
# careagents/checkin/us_core_test_patient.json) carries, by type. That fixture
# is on another branch; these are its values verbatim.
_FIXTURE_CLAIMS = {
    "Patient": _PATIENT,
    "Condition": _PROBLEM,
    "AllergyIntolerance": _UC + "us-core-allergyintolerance",
    "MedicationRequest": _UC + "us-core-medicationrequest",
    "Immunization": _UC + "us-core-immunization",
    "Coverage": _UC + "us-core-coverage",
    "Organization": _UC + "us-core-organization",
    "Practitioner": _UC + "us-core-practitioner",
}


def test_the_check_in_patient_claims_round_trip(client, tenant_headers,
                                                auth_headers):
    """Every claim the fixture carries survives ingest, read and both share
    profiles intact, and its unprofiled second Condition stays unprofiled."""
    resources = [{"resourceType": rtype, "id": f"fx-{rtype.lower()}",
                  "meta": {"profile": [claim]}}
                 for rtype, claim in _FIXTURE_CLAIMS.items()]
    resources.append(_condition(cid="fx-condition-2"))
    _upload(client, resources)
    for rtype, claim in _FIXTURE_CLAIMS.items():
        got = client.get(f"/r6/fhir/{rtype}/fx-{rtype.lower()}",
                         headers=tenant_headers)
        assert got.status_code == 200, (rtype, got.get_data(as_text=True))
        meta = got.get_json()["meta"]
        assert meta["profile"] == [claim], rtype
    got = client.get("/r6/fhir/Condition/fx-condition-2",
                     headers=tenant_headers)
    assert "profile" not in got.get_json()["meta"]
    for profile in ("intake", "deidentified"):
        share = client.post("/r6/fhir/$share-bundle",
                            json={"resource_types": sorted(_FIXTURE_CLAIMS),
                                  "profile": profile},
                            headers=auth_headers)
        assert share.status_code == 200, share.get_data(as_text=True)
        got = {e["resource"]["id"]: e["resource"]["meta"].get("profile")
               for e in share.get_json()["entry"]}
        want = {f"fx-{rtype.lower()}": [claim]
                for rtype, claim in _FIXTURE_CLAIMS.items()}
        want["fx-condition-2"] = None
        assert got == want, profile


# --- idempotence across the two layers ----------------------------------------

def test_both_layers_together_equal_either_alone(app):
    """The share bundle runs `to_fhir_json` then redaction: the claim list
    is the same as one pass of the filter."""
    raw = [_PATIENT + "|7.0.0", _PATIENT + "|9.9.9", _C4DIC, 3,
           "https://ehr.example/p"]
    row = R6Resource(resource_type="Patient",
                     resource_json=json.dumps({"resourceType": "Patient",
                                               "meta": {"profile": raw}}),
                     resource_id="pc-p-1", tenant_id="test-tenant")
    once = keep_profiles(raw)
    stored = row.to_fhir_json()
    assert stored["meta"]["profile"] == once
    assert apply_redaction(stored)["meta"]["profile"] == once
    assert apply_redaction(apply_redaction(stored))["meta"]["profile"] == once
    shared = apply_patient_controlled_redaction(stored, "pc-p-1")
    assert shared["meta"]["profile"] == once
