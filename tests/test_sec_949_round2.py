"""Security re-attack of #949 round 2 (head e574009). Synthetic data only."""

from __future__ import annotations

import json

import pytest

from careagents.agent import _summarize_bundle
from tests.test_beta_acceptance_rows import Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_careagents_first_answer import _first_turn
from tests.test_sec_945_first_answer import _plant

SNOMED = "http://snomed.info/sct"
OID = "urn:oid:2.16.840.1.113883.6.96"
NKA, NKDA, STATUS, PEN = "716186003", "409137002", "1631000175102", "91936005"
NKA_SET = {"716186003", "409137002", "428197003", "428607008", "429625007",
           "716220001", "1003774007"}
PATIENT = {"reference": "Patient/demo-patient-rivera"}
VSYS = "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification"


def _res(rid, *codings, **kw):
    r = {"resourceType": "AllergyIntolerance", "id": rid,
         "code": {"coding": list(codings)}}
    r.update(kw)
    return r


def _sum(*rs, **kw):
    return _summarize_bundle({"entry": [{"resource": r} for r in rs]}, **kw)


def _no_bare(out):
    for i in out:
        if i.get("no_known_allergy_record"):
            continue
        assert "no known" not in json.dumps(i).lower(), i
        assert "patient not asked" not in json.dumps(i).lower(), i


def _resolver(monkeypatch):
    labels = {c: "No known allergy" for c in NKA_SET}
    labels[STATUS] = "Patient not asked"
    monkeypatch.setattr("r6.terminology_resolver.resolve",
                        lambda s, c: labels.get(c) if s == SNOMED else None)


@pytest.mark.parametrize("coding", [
    {"system": OID, "code": NKA}, {"system": " " + OID + "\t", "code": NKA},
    {"system": " " + SNOMED, "code": NKA},
    {"system": SNOMED, "code": " " + NKDA},
    {"system": SNOMED, "code": int(NKDA)},
    {"system": OID, "code": STATUS},
])
def test_engine_spellings_on_real_engine_never_bare(
        cfg, svc, monkeypatch, coding):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    _resolver(monkeypatch)
    _plant(chain, {**_res("r2-a", coding), "patient": PATIENT,
                   "recordedDate": "2024-01-02"})
    out, _ = _first_turn(chain, monkeypatch)
    _no_bare(out["allergies"])


@pytest.mark.parametrize("other", [
    {"system": "urn:oid:2.16.840.1.113883.6.1", "code": NKA},  # LOINC OID
    {"system": "urn:oid:2.16.840.1.113883.6.88", "code": PEN},
    {"display": "x"}, "junk", {"system": SNOMED, "code": ["716186003"]}])
def test_nka_beside_any_other_coding_on_real_engine_never_bare(
        cfg, svc, monkeypatch, other):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    _resolver(monkeypatch)
    _plant(chain, {**_res("r2-b", {"system": OID, "code": NKA}, other),
                   "patient": PATIENT})
    out, _ = _first_turn(chain, monkeypatch)
    _no_bare(out["allergies"])
    assert not any(i.get("no_known_allergy_record") and
                   "lists no known" in i.get("statement", "")
                   for i in out["allergies"])


@pytest.mark.parametrize("vs", [
    "entered-in-error", " refuted ",
    {"coding": [{"system": VSYS, "code": "refuted"}]},
    {"coding": [{"code": "entered-in-error"}]},
    {"coding": [{"system": VSYS, "code": "confirmed"},
                {"system": VSYS, "code": "refuted"}]}])
def test_withdrawn_shapes_never_quoted(vs):
    out = _sum(_res("w", {"system": OID, "code": NKA},
                    verificationStatus=vs, recordedDate="2024-01-02"))
    assert "no known" not in json.dumps(
        [{k: v for k, v in i.items() if k != "note"} for i in out]).lower()


def test_withdrawn_nka_plus_live_nka_plus_real():
    out = _sum(_res("w", {"system": SNOMED, "code": NKA},
                    verificationStatus="refuted"),
               _res("n", {"system": SNOMED, "code": NKDA},
                    recordedDate="2024-01-02"),
               _res("r", {"system": SNOMED, "code": PEN},
                    verificationStatus="entered-in-error"))
    for i in out:
        assert "lists no known" not in i.get("statement", "")


def test_withdrawn_nka_plus_live_nka_quotes_only_live():
    out = _sum(_res("w", {"system": SNOMED, "code": NKA},
                    verificationStatus="refuted"),
               _res("n", {"system": SNOMED, "code": NKDA},
                    recordedDate="2024-01-02"))
    stated = [i["statement"] for i in out if i.get("statement")]
    assert stated == ["Your records include an entry, recorded on "
                      "2024-01-02, that lists no known drug allergies. "
                      "Please confirm with your doctor or pharmacist."]


def test_withdrawn_alone_does_not_read_as_status_or_absence():
    out = _sum(_res("w", {"system": SNOMED, "code": NKA},
                    verificationStatus={"coding": [{"code": "refuted"}]}))
    blob = json.dumps(out).lower()
    assert "lists no known" not in blob
    assert "don't say whether" not in blob


def test_nka_concept_outside_allergyintolerance_on_real_engine(
        cfg, svc, monkeypatch):  # noqa: F811
    """Feeds that put 'No known allergies' on the problem list or as an
    Observation: the engine labels it and it reaches the model as a name."""
    chain = Chain(cfg, svc, monkeypatch)
    _resolver(monkeypatch)
    res = {"resourceType": "Condition", "id": "r2-c", "subject": PATIENT,
           "code": {"coding": [{"system": SNOMED, "code": NKA}]}}
    _plant(chain, res)
    out, _ = _first_turn(chain, monkeypatch)
    blob = json.dumps(out).lower()
    assert "no known allergy" not in blob, out


def test_nka_observation_as_the_engine_labels_it_via_search_records():
    """search_records(Observation) on an engine-labelled NKA Observation."""
    obs = {"resourceType": "Observation", "id": "o", "status": "final",
           "code": {"text": "No known allergy", "coding": [{
               "system": SNOMED, "code": NKA, "display": "No known allergy"}]}}
    assert "no known allergy" not in json.dumps(_sum(obs)).lower()
