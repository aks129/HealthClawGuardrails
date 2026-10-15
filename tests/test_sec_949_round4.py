"""Security re-attack of #949 round 4 (head e8688f9): dropping records coded
only as an absence. Synthetic data only."""

from __future__ import annotations

import json

import pytest

from careagents.agent import _summarize_bundle
from r6.brief import engine

SNOMED = "http://snomed.info/sct"
OID = "urn:oid:2.16.840.1.113883.6.96"
NKA, STATUS, DM = "716186003", "1631000175102", "44054006"
ACTIVE = {"coding": [{"code": "active"}]}


def _cond(rid, *codings, **kw):
    r = {"resourceType": "Condition", "id": rid, "clinicalStatus": ACTIVE,
         "code": {"coding": list(codings)}}
    r.update(kw)
    return r


def _c(system, code):
    return {"system": system, "code": code}


def _b(*rs, total=None):
    b = {"entry": [{"resource": r} for r in rs]}
    if total is not None:
        b["total"] = total
    return b


KEEP = [
    [_c(SNOMED, NKA), _c(SNOMED, DM)],
    [_c(OID, NKA), _c("urn:oid:2.16.840.1.113883.6.90", "E11.9")],
    [_c(SNOMED, NKA), _c("http://example.org/local", "X1")],
    [_c(SNOMED, NKA), "junk"],
    [_c(SNOMED, NKA), {"display": "x"}],
    [_c(SNOMED, NKA), _c(SNOMED, True)],
    [_c(SNOMED, NKA), _c(["x"], DM)],
    [_c("HTTP://SNOMED.INFO/SCT", NKA)],
    [_c("https://snomed.info/sct", NKA)],
    [_c(SNOMED, NKA + "x")],
    [],
]


@pytest.mark.parametrize("codings", KEEP)
def test_a_record_with_anything_else_is_never_dropped(codings):
    out = _summarize_bundle(_b(_cond("c", *codings)))
    assert [i for i in out if i.get("type") == "Condition"], out
    assert engine.build_problems([_cond("c", *codings)]), codings


# A string code or string coding used to raise AttributeError here and
# fail the turn; fixed in #949, so these run as ordinary cases.
@pytest.mark.parametrize("code", [
    None, {"coding": "x"}, {"coding": {}},
    {"text": "x"}, [], "716186003"])
def test_malformed_code_elements_are_never_dropped(code):
    res = {"resourceType": "Condition", "id": "c", "clinicalStatus": ACTIVE,
           "code": code}
    out = _summarize_bundle(_b(res))
    assert [i for i in out if i.get("type") == "Condition"], out
    assert engine.build_problems([res])


@pytest.mark.parametrize("codings", [
    [_c(OID, NKA)], [_c(" " + SNOMED, STATUS)], [_c(SNOMED, int(NKA))],
    [_c(SNOMED, NKA), _c(OID, "409137002")]])
def test_absence_only_is_dropped_and_never_an_allergy_statement(codings):
    out = _summarize_bundle(_b(_cond("c", *codings)))
    assert out == []
    blob = json.dumps(out).lower()
    assert "no known" not in blob and "no allergies" not in blob


@pytest.mark.parametrize("n_drop,page,total", [
    (5, 50, 60), (1, 50, 51), (50, 50, 51), (3, 10, None), (2, 5, 6)])
def test_subtraction_never_makes_a_cut_list_read_complete(n_drop, page,
                                                         total):
    rs = [_cond(f"d{k}", _c(SNOMED, NKA)) for k in range(n_drop)]
    rs += [_cond(f"r{k}", _c(SNOMED, DM)) for k in range(page - n_drop)]
    if total is None:  # no total: more entries than the limit
        rs += [_cond("extra", _c(SNOMED, DM))]
    out = _summarize_bundle(_b(*rs, total=total), limit=page)
    assert out and out[-1].get("truncated") is True, out[-3:]


@pytest.mark.parametrize("total", [-5, True, 0, "999", 10**12])
def test_odd_totals(total):
    rs = [_cond("d", _c(SNOMED, NKA)), _cond("r", _c(SNOMED, DM))]
    out = _summarize_bundle(_b(*rs, total=total), limit=50)
    real = [i for i in out if i.get("type") == "Condition"]
    assert len(real) == 1
    if total == 10**12:
        assert out[-1].get("truncated") is True


def test_only_absences_does_not_hide_a_real_lab_beyond_the_cap():
    obs = [{"resourceType": "Observation", "id": f"a{k}", "status": "final",
            "effectiveDateTime": f"2024-02-{k + 1:02d}",
            "code": {"coding": [_c(SNOMED, NKA)]}} for k in range(12)]
    real = {"resourceType": "Observation", "id": "real", "status": "final",
            "effectiveDateTime": "2020-01-01",
            "code": {"coding": [_c("http://loinc.org", "4548-4")]},
            "valueQuantity": {"value": 6.1, "unit": "%",
                              "system": "http://unitsofmeasure.org",
                              "code": "%"}}
    fields = engine.build_labs(obs + [real])
    assert [f for f in fields if f.source_id == "real"]


def test_value_only_observation_with_absence_value_is_kept_unnamed():
    o = {"resourceType": "Observation", "id": "v", "status": "final",
         "effectiveDateTime": "2024-01-02",
         "code": {"coding": [_c("http://loinc.org", "52473-6")]},
         "valueCodeableConcept": {"text": "QZXCANARY Zelda",
                                  "coding": [_c(OID, NKA)]}}
    [f] = engine.build_labs([o])
    blob = json.dumps(f.__dict__, default=str)
    assert "no known" not in blob.lower() and "QZXCANARY" not in blob
    assert f.value.startswith(engine.UNLABELLED)


@pytest.mark.parametrize("eff", [
    "2024-03-05", "2024-03-05 QZXCANARY Zelda Quornwhistle", "QZXCANARY",
    {"x": "QZXCANARY"}])
def test_unreadable_note_carries_no_upstream_text(eff):
    res = {"resourceType": "Observation", "id": "u", "status": "final",
           "effectiveDateTime": eff,
           "code": {"text": "QZXCANARY code text",
                    "coding": [{"system": "urn:x-local", "code": "Z9",
                                "display": "QZXCANARY display"}]},
           "note": [{"text": "QZXCANARY note"}]}
    # upstream text is stripped by redaction before this; prove the note
    # itself adds none even if it were not.
    res["code"].pop("text")
    res["code"]["coding"][0].pop("display")
    [item] = _summarize_bundle(_b(res))
    assert item.get("unreadable") and "note" in item
    assert "QZXCANARY" not in json.dumps(item)
    assert "Quornwhistle" not in json.dumps(item)
