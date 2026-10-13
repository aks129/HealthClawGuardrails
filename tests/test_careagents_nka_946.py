"""A coded "no known allergy" record reaches the model as what it is (#946).

Production's terminology resolver labels SNOMED 716186003 "No known
allergy", and that label reached the model as `name: "No known allergy"`:
a bare allergy-shaped fact, undated. The owner's decision (option a): the
record is quoted as a dated entry in the records, never as a fact about the
person, always with a line to confirm with a doctor or pharmacist. Beside a
recorded allergy, or in a list cut short, it never says "no known" at all.

AGENTS.md: "No known allergies" is never inferred. Synthetic data only.
"""

from __future__ import annotations

import json
import logging

import pytest

from careagents.agent import _is_allergy_status, _summarize_bundle
from tests.test_beta_acceptance_rows import Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_careagents_first_answer import _first_turn
from tests.test_sec_945_first_answer import (
    CANARY, _allergy, _audit_details, _free_text, _no_canary, _plant,
    _turn_capturing)

SNOMED = "http://snomed.info/sct"
NKA = "716186003"
NKDA = "409137002"
STATUS = "1631000175102"
PENICILLIN = "91936005"

#: The closed set: SNOMED 716186003 and its descendants, expanded on
#: tx.fhir.org (isa/716186003, 2026-10-13), each with the words our own
#: label table gives it.
NKA_SET = {
    "716186003": "no known allergies",
    "409137002": "no known drug allergies",
    "428197003": "no known insect allergies",
    "428607008": "no known environmental allergies",
    "429625007": "no known food allergies",
    "716220001": "no known animal allergies",
    "1003774007": "no known latex allergies",
}

CONFIRM = "Please confirm with your doctor or pharmacist."


def _nka(rid="n1", code=NKA, **fields):
    res = {"resourceType": "AllergyIntolerance", "id": rid,
           "code": {"coding": [{"system": SNOMED, "code": code,
                                # The production resolver's label.
                                "display": "No known allergy"}]}}
    res.update(fields)
    return res


def _real(rid="r1", code=PENICILLIN):
    return {"resourceType": "AllergyIntolerance", "id": rid,
            "code": {"coding": [{"system": SNOMED, "code": code}],
                     "text": "Penicillin"}}


def _bundle(*resources, total=None):
    b = {"entry": [{"resource": r} for r in resources]}
    if total is not None:
        b["total"] = total
    return b


def _nka_items(out):
    return [i for i in out if i.get("no_known_allergy_record")]


def _quoted(item):
    """What the model may say: everything but its instruction, which names
    "no known allergies" only to forbid it (asserted separately)."""
    if item.get("no_known_allergy_record"):
        assert "never say" in item["note"].lower()
    return {k: v for k, v in item.items() if k != "note"}


# --- the record is quoted as a dated entry ----------------------------------

@pytest.mark.parametrize("code,words", sorted(NKA_SET.items()))
def test_each_nka_code_is_a_dated_entry_not_a_name(code, words):
    out = _summarize_bundle(_bundle(_nka(code=code,
                                         recordedDate="2024-03-05T10:00:00Z")))
    [item] = out
    assert item["no_known_allergy_record"] is True
    assert "name" not in item
    assert item["statement"] == (
        f"Your records include an entry, recorded on 2024-03-05, that lists "
        f"{words}. {CONFIRM}")
    assert item["date"] == "2024-03-05"
    assert item["date_kind"] == "recorded"
    note = item["note"].lower()
    assert "never say they have no allergies" in note
    assert "doctor or pharmacist" in note


def test_the_resolver_label_never_reaches_the_model():
    out = json.dumps(_summarize_bundle(_bundle(_nka(recordedDate="2024-03-05"))))
    assert "No known allergy" not in out
    assert '"name"' not in out


def test_date_falls_back_from_recorded_to_onset_to_last_updated():
    onset = _summarize_bundle(_bundle(_nka(
        onsetDateTime="2019-06-01", meta={"lastUpdated": "2026-10-01T00:00:00Z"})))
    assert onset[0]["date"] == "2019-06-01"
    assert onset[0]["date_kind"] == "onset"
    assert "dated 2019-06-01" in onset[0]["statement"]

    # The engine's lastUpdated is when the entry arrived HERE, not when the
    # clinic recorded it: it is never passed off as a recorded date.
    updated = _summarize_bundle(_bundle(_nka(
        meta={"lastUpdated": "2026-10-01T08:00:00Z"})))
    [item] = updated
    assert item["date"] == "2026-10-01"
    assert item["date_kind"] == "last_updated"
    assert "recorded on" not in item["statement"]
    assert "last updated in your records on 2026-10-01" in item["statement"]
    assert "don't show when it was first recorded" in item["statement"]


def test_no_date_at_all_says_the_records_do_not_show_when():
    [item] = _summarize_bundle(_bundle(_nka()))
    assert "date" not in item
    assert item["statement"] == (
        "Your records include an entry that lists no known allergies; the "
        f"records don't show when it was recorded. {CONFIRM}")


@pytest.mark.parametrize("bad", ["yesterday", "2024-13-45", 20240305,
                                 "Zelda Quornwhistle 2024-03-05", ["2024"]])
def test_an_unparseable_date_is_not_a_date(bad):
    [item] = _summarize_bundle(_bundle(_nka(recordedDate=bad)))
    assert "date" not in item
    assert "don't show when" in item["statement"]
    assert "Quornwhistle" not in json.dumps(item)


def test_onset_string_is_never_used_as_a_date():
    [item] = _summarize_bundle(_bundle(_nka(onsetString="2024-03-05")))
    assert "date" not in item


# --- matching: explicit SNOMED system and code, every coding ----------------

@pytest.mark.parametrize("codings", [
    [{"system": "http://snomed.info/sct/", "code": NKA}],
    [{"system": "https://snomed.info/sct", "code": NKA}],
    [{"code": NKA}],
    [{"system": SNOMED, "code": NKA + " "}],
    [{"system": SNOMED, "code": int(NKA)}],
    [{"system": SNOMED, "code": NKA}, {"system": SNOMED, "code": PENICILLIN}],
    [{"system": SNOMED, "code": NKA}, {"system": "urn:x", "code": "Z"}],
    [{"system": SNOMED, "code": NKA}, "not-a-coding"],
    [{"system": SNOMED, "code": NKA}, {"display": "No known allergy"}],
])
def test_anything_but_an_exact_nka_coding_set_is_a_real_allergy(codings):
    res = {"resourceType": "AllergyIntolerance", "id": "x",
           "code": {"coding": codings}}
    [item] = _summarize_bundle(_bundle(res))
    assert not item.get("no_known_allergy_record")
    assert not item.get("not_an_allergy")
    assert "no known" not in json.dumps(item).lower()


def test_text_alone_saying_no_known_allergy_is_not_an_nka_record():
    res = {"resourceType": "AllergyIntolerance", "id": "x",
           "code": {"text": "No known allergies"}}
    [item] = _summarize_bundle(_bundle(res))
    assert not item.get("no_known_allergy_record")


def test_two_nka_codings_on_one_record_quote_both():
    res = {"resourceType": "AllergyIntolerance", "id": "x",
           "recordedDate": "2024-03-05",
           "code": {"coding": [{"system": SNOMED, "code": NKA},
                               {"system": SNOMED, "code": NKDA}]}}
    [item] = _summarize_bundle(_bundle(res))
    assert "lists no known allergies and no known drug allergies" in (
        item["statement"])


def test_nka_beside_a_status_code_on_one_record_is_the_weaker_status():
    """A record that says both "not asked" and "no known allergy" does not
    say there are none: it reads as status, the records don't say."""
    res = {"resourceType": "AllergyIntolerance", "id": "x",
           "code": {"coding": [{"system": SNOMED, "code": NKA},
                               {"system": SNOMED, "code": STATUS}]}}
    assert _is_allergy_status(res)
    [item] = _summarize_bundle(_bundle(res))
    assert item["not_an_allergy"] is True
    assert "no known" not in json.dumps(item).lower()


@pytest.mark.parametrize("code", sorted(NKA_SET))
def test_nka_alone_is_not_a_status_record(code):
    nka = _nka(code=code)
    assert not _is_allergy_status(nka)
    blob = json.dumps(_summarize_bundle(_bundle(nka))).lower()
    assert "not_an_allergy" not in blob
    assert "you have no allergies" not in blob
    assert "don't say whether you have allergies" not in blob


# --- beside a real allergy, the real allergy wins ---------------------------

@pytest.mark.parametrize("order", ["nka-first", "real-first"])
def test_nka_beside_a_real_allergy_never_says_no_known(order):
    records = [_nka(recordedDate="2020-01-02"), _real()]
    if order == "real-first":
        records.reverse()
    out = _summarize_bundle(_bundle(*records))
    [item] = _nka_items(out)
    blob = json.dumps(_quoted(item)).lower()
    assert "no known" not in blob
    assert item["statement"] == (
        "Your records also include an entry, recorded on 2020-01-02, that "
        "disagrees with the allergies listed. Please confirm your allergies "
        "with your doctor or pharmacist.")
    assert "name the allergies listed" in item["note"]
    assert "never say the person has no known allergies" in item["note"]
    real = [i for i in out if not i.get("no_known_allergy_record")]
    assert real and real[0]["type"] == "AllergyIntolerance"


def test_an_unreadable_real_allergy_still_wins():
    uncoded = {"resourceType": "AllergyIntolerance", "id": "u1",
               "code": {"text": "Zelda's allergy"}}
    out = _summarize_bundle(_bundle(_nka(recordedDate="2020-01-02"), uncoded))
    [item] = _nka_items(out)
    assert "no known" not in json.dumps(_quoted(item)).lower()
    assert "disagrees" in item["statement"]


def test_a_status_record_does_not_make_a_conflict():
    status = _allergy("s1", (SNOMED, STATUS))
    out = _summarize_bundle(_bundle(_nka(recordedDate="2020-01-02"), status))
    [item] = _nka_items(out)
    assert "lists no known allergies" in item["statement"]
    # The status record no longer says the records are silent: one of them
    # says something.
    [st] = [i for i in out if i.get("not_an_allergy")]
    assert "statement" not in st


# --- a cut list never carries the statement as complete --------------------

def test_truncated_list_drops_the_nka_statement():
    out = _summarize_bundle(_bundle(_nka(recordedDate="2020-01-02"),
                                    total=7))
    [item] = _nka_items(out)
    assert "no known" not in json.dumps(_quoted(item)).lower()
    assert "longer than can be shown here" in item["statement"]
    assert out[-1].get("truncated") is True


def test_limit_cut_list_drops_the_nka_statement():
    out = _summarize_bundle(_bundle(_nka(recordedDate="2020-01-02"),
                                    _nka("n2")), limit=1)
    [item] = _nka_items(out)
    assert "no known" not in json.dumps(_quoted(item)).lower()


# --- cross-layer: the real engine with production's label ------------------

def _resolver(monkeypatch):
    monkeypatch.setattr(
        "r6.terminology_resolver.resolve",
        lambda s, c: "No known allergy" if c in NKA_SET else None)


def test_real_engine_nka_reaches_the_model_as_a_dated_entry(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    _resolver(monkeypatch)
    _plant(chain, {**_nka("nka-946-a"), "recordedDate": "2023-11-20",
                   "patient": {"reference": "Patient/demo-patient-rivera"}})
    out, _system = _first_turn(chain, monkeypatch)
    [item] = out["allergies"]
    assert item["no_known_allergy_record"] is True
    assert item["statement"] == (
        "Your records include an entry, recorded on 2023-11-20, that lists "
        f"no known allergies. {CONFIRM}")
    assert "No known allergy" not in json.dumps(out)


def test_real_engine_nka_without_a_clinic_date_uses_the_arrival_date_as_such(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    _resolver(monkeypatch)
    _plant(chain, {**_nka("nka-946-b"),
                   "patient": {"reference": "Patient/demo-patient-rivera"}})
    out, _system = _first_turn(chain, monkeypatch)
    [item] = out["allergies"]
    assert item["date_kind"] == "last_updated"
    assert "recorded on" not in item["statement"]
    assert "don't show when it was first recorded" in item["statement"]


def test_real_engine_nka_beside_penicillin_never_says_no_known(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    _resolver(monkeypatch)
    _plant(chain,
           {**_nka("nka-946-c"), "recordedDate": "2019-02-03",
            "patient": {"reference": "Patient/demo-patient-rivera"}},
           _allergy("pen-946-c", (SNOMED, PENICILLIN)))
    out, _system = _first_turn(chain, monkeypatch)
    blob = json.dumps([_quoted(i) for i in out["allergies"]]).lower()
    assert "no known allergies" not in blob
    assert "lists no known" not in blob
    [item] = _nka_items(out["allergies"])
    assert "disagrees with the allergies listed" in item["statement"]


def test_real_engine_canaries_in_an_nka_record_leak_nothing(
        cfg, svc, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch)
    _resolver(monkeypatch)
    nka = _free_text(_allergy("nka-946-d", (SNOMED, NKA)), "nka")
    nka["onsetString"] = f"{CANARY}-onset since Zelda Quornwhistle was born"
    nka["recordedDate"] = "2022-08-09"
    nkda = _free_text(_allergy("nka-946-e", (SNOMED, NKDA)), "nkda")
    _plant(chain, nka, nkda)
    model, sse, events = _turn_capturing(chain, monkeypatch)
    _no_canary(model, sse, events, caplog.text, _audit_details(chain))
    assert "recorded on 2022-08-09, that lists no known allergies" in model
    assert "that lists no known drug allergies" in model


def test_the_prompt_lets_a_dated_entry_be_quoted_and_nothing_more():
    """SAFETY_CORE forbade "no known allergies" outright, which a model
    could read as forbidding the dated entry too; it now names the one way
    such a record is said, and still forbids the claim."""
    from careagents.personas import SAFETY_CORE
    core = " ".join(SAFETY_CORE.split()).lower()
    assert 'never claim the person has "no known allergies"' in core
    assert "as a dated entry in their records, never as a fact about them" in (
        core)
    assert "confirm with their doctor or pharmacist" in core
