"""Round-two attacks on the StoredResource carrier (PR #956, head e8130a2).

The stamp travels as a Python attribute on `StoredResource`; redaction
strips every record-source tag and re-stamps only from that attribute.
These tests attack the carrier and the strip. Synthetic data only.
"""

from __future__ import annotations

import copy
import json
import pickle

import pytest

from models import db
from r6.models import R6Resource
from r6.record_source import RECORD_SOURCE_SYSTEM, StoredResource
from r6.redaction import (apply_patient_controlled_redaction,
                          apply_redaction)

_CANARY_URI = "https://portal.example.invalid/patients/Rosa-Kowalski-1971"


def _ours(tags):
    return [t for t in tags or [] if isinstance(t, dict)
            and t.get("system") == RECORD_SOURCE_SYSTEM]


def _cond(cid="r2-c-1"):
    return {"resourceType": "Condition", "id": cid,
            "subject": {"reference": "Patient/r2-p-1"}}


# --- no request can mint a carrier ---------------------------------------------

@pytest.mark.parametrize("payload", [
    {"record_source": "fasten"},
    {"__class__": "StoredResource", "record_source": "fasten"},
    {"meta": {"record_source": "fasten"}},
])
def test_json_cannot_produce_a_stamp(payload):
    res = dict(_cond(), **payload)
    for out in (apply_redaction(res),
                apply_patient_controlled_redaction(res, "r2-p-1")):
        assert _ours(out.get("meta", {}).get("tag")) == []


def test_a_dict_with_an_off_list_carrier_is_never_stamped():
    res = StoredResource(_cond())
    res.record_source = "Rosa Kowalski"
    assert _ours(apply_redaction(res).get("meta", {}).get("tag")) == []


# --- copies keep or lose the stamp in the safe direction ------------------------

def test_copies_of_a_stored_resource(app):
    from r6.fasten.ingester import _ingest_one
    _ingest_one(_cond(), "test-tenant", source="fasten")
    db.session.commit()
    stored = R6Resource.query.filter_by(id="r2-c-1").one().to_fhir_json()

    # deepcopy and pickle keep the carrier: still this row's own channel.
    for twin in (copy.deepcopy(stored), pickle.loads(pickle.dumps(stored))):
        assert _ours(apply_redaction(twin)["meta"]["tag"]) == [
            {"system": RECORD_SOURCE_SYSTEM, "code": "fasten"}]
    # A plain-dict copy and a JSON round trip drop it: says nothing.
    for plain in (dict(stored), json.loads(json.dumps(stored))):
        assert _ours(apply_redaction(plain).get("meta", {}).get("tag")) == []


def test_redaction_does_not_mutate_or_reuse_its_input_carrier(app):
    """A carrier from row A must never stamp a different row's content:
    redaction returns a new plain dict, never the input object."""
    from r6.fasten.ingester import _ingest_one
    _ingest_one(_cond("r2-a"), "test-tenant", source="fasten")
    _ingest_one(_cond("r2-b"), "other-tenant", source="upload")
    db.session.commit()
    a = R6Resource.query.filter_by(id="r2-a").one().to_fhir_json()
    out = apply_redaction(a)
    assert type(out) is dict and not hasattr(out, "record_source")
    assert out is not a


# --- nothing legitimate is lost --------------------------------------------------

def test_non_meta_source_keys_survive(app):
    """Only meta.source is dropped; a resource element called `source`
    (AuditEvent.source, Provenance-style) and other systems' tags stay."""
    res = {"resourceType": "AuditEvent", "id": "ae-1",
           "source": {"observer": {"reference": "Device/d1"}},
           "meta": {"source": _CANARY_URI,
                    "tag": [{"system": "https://healthclaw.io/tags",
                             "code": "keep-me"}]}}
    out = apply_redaction(res)
    assert out["source"] == {"observer": {"reference": "Device/d1"}}
    assert out["meta"]["tag"] == [{"system": "https://healthclaw.io/tags",
                                   "code": "keep-me"}]
    assert "source" not in out["meta"]


# --- near-miss spellings of our system -------------------------------------------

@pytest.mark.parametrize("system", [
    RECORD_SOURCE_SYSTEM + "/",
    RECORD_SOURCE_SYSTEM + " ",
    RECORD_SOURCE_SYSTEM.upper(),
    RECORD_SOURCE_SYSTEM.replace("https://", "http://"),
])
def test_near_miss_systems_are_not_ours(system):
    """Equality is exact on the strip side. Recorded so a consumer that
    normalises the system (strip/lower/trailing slash) knows it would read
    these as ours: they pass through redaction verbatim."""
    res = dict(_cond(), meta={"tag": [{"system": system, "code": "fasten"}]})
    out = apply_redaction(res)
    assert _ours(out["meta"]["tag"]) == []
    assert out["meta"]["tag"][0]["system"] == system  # passes through
