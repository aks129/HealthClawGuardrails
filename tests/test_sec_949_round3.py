"""Security re-attack of #949 round 3 (head 9199703): the brief path.
Synthetic data only."""

from __future__ import annotations

import json
import logging

import pytest

from r6.brief import engine
from tests.test_beta_acceptance_rows import TENANT, Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_sec_945_first_answer import (
    _audit_details, _free_text, _no_canary, _plant)

SNOMED = "http://snomed.info/sct"
OID = "urn:oid:2.16.840.1.113883.6.96"
NKA_SET = {"716186003", "409137002", "428197003", "428607008", "429625007",
           "716220001", "1003774007", "1631000175102"}
PATIENT = {"reference": "Patient/demo-patient-rivera"}

SPELLINGS = [(OID, "716186003"), (" " + OID + "\t", "409137002"),
             (" " + SNOMED, "429625007"), (SNOMED, " 716186003"),
             (SNOMED, 1003774007), (OID, "1631000175102")]


def _label_all(monkeypatch):
    monkeypatch.setattr(
        "r6.terminology_resolver.resolve",
        lambda s, c: ("No known allergy" if s == SNOMED and c in NKA_SET
                      else ("Diabetes" if c == "44054006" else None)))


def _cc(system, code):
    return {"coding": [{"system": system, "code": code}]}


@pytest.mark.parametrize("system,code", SPELLINGS)
def test_every_brief_label_path_refuses_absence_spellings(
        monkeypatch, system, code):
    _label_all(monkeypatch)
    cond = {"resourceType": "Condition", "id": "c",
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": _cc(system, code)}
    lab = {"resourceType": "Observation", "id": "o", "status": "final",
           "effectiveDateTime": "2024-01-02", "code": _cc(system, code)}
    val = {"resourceType": "Observation", "id": "v", "status": "final",
           "effectiveDateTime": "2024-01-02",
           "code": _cc("http://loinc.org", "4548-4"),
           "valueCodeableConcept": _cc(system, code)}
    med = {"resourceType": "MedicationRequest", "id": "m",
           "status": "active", "medicationCodeableConcept": _cc(system, code)}
    enc = {"resourceType": "Encounter", "id": "e",
           "period": {"start": "2024-01-02"}, "type": [_cc(system, code)]}
    blob = json.dumps([f.__dict__ for f in (
        engine.build_problems([cond]) + engine.build_labs([lab, val])
        + engine.build_medications([med]) + engine.build_visits([enc]))],
        default=str).lower()
    assert "no known" not in blob, blob


def test_absence_beside_a_real_code_drops_the_whole_label_failsafe(
        monkeypatch):
    _label_all(monkeypatch)
    cond = {"resourceType": "Condition", "id": "c",
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": {"coding": [{"system": SNOMED, "code": "44054006"},
                                {"system": OID, "code": "716186003"}]}}
    [field] = engine.build_problems([cond])
    assert field.label == engine.UNLABELLED


@pytest.mark.parametrize("coding", [{"system": SNOMED, "code": True},
                                    {"system": ["x"], "code": "716186003"},
                                    "junk", None])
def test_odd_codings_do_not_crash_the_brief(monkeypatch, coding):
    _label_all(monkeypatch)
    cond = {"resourceType": "Condition", "id": "c",
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": {"coding": [coding, {"system": OID,
                                         "code": "716186003"}]}}
    [field] = engine.build_problems([cond])
    assert "no known" not in field.label.lower()


def test_real_engine_brief_with_canaries_leaks_nothing(
        cfg, svc, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    from careagents.agent import _execute_tool
    chain = Chain(cfg, svc, monkeypatch)
    _label_all(monkeypatch)
    cond = _free_text({"resourceType": "Condition", "id": "r3-c",
                       "clinicalStatus": {"coding": [{"code": "active"}]},
                       "code": _cc(OID, "716186003")}, "cond")
    obs = _free_text({"resourceType": "Observation", "id": "r3-o",
                      "status": "final", "effectiveDateTime": "2024-01-02",
                      "code": _cc("http://loinc.org", "4548-4"),
                      "valueCodeableConcept": {
                          "text": "QZXCANARY value Zelda Quornwhistle",
                          "coding": [{"system": " " + SNOMED,
                                      "code": "409137002",
                                      "display": "QZXCANARY disp"}]}},
                     "obs")
    _plant(chain, cond, obs)
    out = _execute_tool(chain.hc, TENANT, "appointment_brief", {}, [])
    assert "no known" not in out.lower()
    _no_canary(out, caplog.text, _audit_details(chain))
    assert "Condition" in _audit_details(chain)
