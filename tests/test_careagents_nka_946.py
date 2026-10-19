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
LOINC = "http://loinc.org"
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
    [{"system": SNOMED, "code": NKA + "x"}],
    [{"system": SNOMED, "code": True}],
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


@pytest.mark.parametrize("system,code", [
    ("urn:oid:2.16.840.1.113883.6.96", NKA), (" " + SNOMED, NKA),
    (SNOMED + "\n", NKA), (SNOMED, " " + NKA), (SNOMED, NKA + "\n"),
    (SNOMED, int(NKA))])
def test_engine_spellings_of_nka_are_nka(system, code):
    """Keyed as the engine keys a coding before it labels it (#949 F1)."""
    res = {"resourceType": "AllergyIntolerance", "id": "x",
           "code": {"coding": [{"system": system, "code": code}],
                    "text": "No known allergy"}}
    [item] = _summarize_bundle(_bundle(res))
    assert item.get("no_known_allergy_record") is True
    assert "name" not in item


def test_snomed_spellings_match_the_engine():
    """CareAgents imports nothing from the engine, so the SNOMED aliases
    are mirrored; this holds the mirror equal to r6.terminology."""
    from careagents.agent import _SNOMED_SYSTEMS
    from r6 import terminology
    assert _SNOMED_SYSTEMS == {
        k for k, v in terminology._SYSTEM_ALIASES.items()
        if v == terminology.SNOMED}
    assert terminology.SNOMED == SNOMED


def test_loinc_spellings_match_the_engine():
    from careagents.agent import _LOINC_SYSTEMS
    from r6 import terminology
    assert _LOINC_SYSTEMS == {
        k for k, v in terminology._SYSTEM_ALIASES.items()
        if v == terminology.LOINC}
    assert terminology.LOINC == LOINC


#: LOINC answers that state an absence (the engine's LOINC_ABSENCE_ANSWERS),
#: each with the words our own table gives it.
LOINC_NONE = {"LA10030-7": "allergies as none known",
              "LA9134-3": "allergies as none known",
              "LA4715-4": "allergies as none or not known"}


@pytest.mark.parametrize("system", [LOINC, "urn:oid:2.16.840.1.113883.6.1",
                                    " https://loinc.org"])
@pytest.mark.parametrize("code,words", sorted(LOINC_NONE.items()))
def test_a_loinc_none_known_record_is_a_dated_entry_not_a_name(
        system, code, words):
    """#957 re-QA: an AllergyIntolerance coded LOINC LA10030-7 missed the
    absence sets, so the engine's qualified text became its name and the
    model said it had no date, though recordedDate was set."""
    res = {"resourceType": "AllergyIntolerance", "id": "l1",
           "recordedDate": "2021-09-14",
           "code": {"coding": [{"system": system, "code": code}],
                    "text": "Recorded entry: 'none known'. Not a confirmed "
                            "absence; confirm with a clinician."}}
    [item] = _summarize_bundle(_bundle(res))
    assert item["no_known_allergy_record"] is True
    assert "name" not in item
    assert item["statement"] == (
        f"Your records include an entry, recorded on 2021-09-14, that lists "
        f"{words}. {CONFIRM}")
    assert item["date"] == "2021-09-14"
    assert item["date_kind"] == "recorded"
    note = item["note"].lower()
    assert "never say they have no allergies" in note
    assert "doctor or pharmacist" in note


@pytest.mark.parametrize("coding", [
    {"system": SNOMED, "code": NKA}, {"system": SNOMED, "code": STATUS}])
@pytest.mark.parametrize("other", [
    "not-a-coding", {"display": "x"}, {"system": SNOMED, "code": PENICILLIN},
    {"system": "urn:x", "code": "Z"}])
def test_an_absence_code_is_never_a_name_whatever_sits_beside_it(
        coding, other):
    """Backstop (#949 F1): a record that fails the checks above is still
    never named by text the engine filled with that concept's label."""
    res = {"resourceType": "AllergyIntolerance", "id": "x",
           "code": {"coding": [dict(coding, display="No known allergy"),
                               other],
                    "text": "No known allergy"}}
    [item] = _summarize_bundle(_bundle(res))
    assert "no known" not in json.dumps(_quoted(item)).lower()
    # None of these has a terminology label beside the absence code, so the
    # allergy is said plainly, never named by the absence (FHI-48).
    assert item.get("name") == "an allergy, name not recognised"
    assert item["unreadable"] is True


@pytest.mark.parametrize("vs", [
    {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/"
                 "allergyintolerance-verification", "code": "refuted"}]},
    {"coding": [{"code": " entered-in-error"}]}, "entered-in-error"])
def test_a_withdrawn_nka_entry_is_never_sent(vs):
    """Real-model QA on #949: handed a withdrawn entry with a "do not
    mention it" note, the model mentioned it and disclosed the note. What
    must not reach the person is not sent at all."""
    out = _summarize_bundle(_bundle(_nka(recordedDate="2024-03-05",
                                         verificationStatus=vs)))
    blob = json.dumps(out).lower()
    for word in ("withdrawn", "entered in error", "entered-in-error",
                 "refuted", "2024-03-05", "no known", "do not mention"):
        assert word not in blob
    # An empty allergy list is never absence: the one item left says so.
    [marker] = out
    assert "type" not in marker
    assert "does not mean they have no allergies" in marker["note"]
    assert not _nka_items(out)


def test_a_withdrawn_nka_entry_is_not_counted_as_more_records():
    withdrawn = _nka(verificationStatus="refuted")
    live = _nka("n2", code=NKDA, recordedDate="2024-01-02")
    out = _summarize_bundle(_bundle(withdrawn, live, total=2))
    assert [i.get("statement") for i in out] == [
        "Your records include an entry, recorded on 2024-01-02, that lists "
        f"no known drug allergies. {CONFIRM}"]


def test_a_withdrawn_real_allergy_still_blocks_the_nka_statement():
    """Chosen: a withdrawn REAL allergy is kept, as before. Dropping it
    would let a "no known allergies" entry beside it be quoted, the
    unsafe direction."""
    real = dict(_real(), verificationStatus="entered-in-error")
    out = _summarize_bundle(_bundle(_nka(recordedDate="2024-01-02"), real))
    [item] = _nka_items(out)
    assert "no known" not in item["statement"].lower()
    assert any(i.get("name") for i in out)


def test_real_engine_withdrawn_nka_never_reaches_the_model(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    _resolver(monkeypatch)
    _plant(chain, {**_nka("nka-946-w"), "recordedDate": "2021-05-06",
                   "verificationStatus": {"coding": [
                       {"code": "entered-in-error"}]},
                   "patient": {"reference": "Patient/demo-patient-rivera"}})
    out, _system = _first_turn(chain, monkeypatch)
    blob = json.dumps(out).lower()
    for word in ("withdrawn", "entered", "refuted", "2021-05-06",
                 "no known allerg", "do not mention"):
        assert word not in blob
    assert "does not mean they have no allergies" in blob


def test_a_withdrawn_nka_entry_leaves_the_status_statement_alone():
    status = _allergy("s1", (SNOMED, STATUS))
    withdrawn = _nka(verificationStatus={"coding": [{"code": "refuted"}]})
    out = _summarize_bundle(_bundle(withdrawn, status))
    [st] = [i for i in out if i.get("not_an_allergy")]
    assert st["statement"] == "Your records don't say whether you have allergies."


def test_a_confirmed_nka_entry_is_still_quoted():
    [item] = _summarize_bundle(_bundle(_nka(
        recordedDate="2024-03-05",
        verificationStatus={"coding": [{"code": "confirmed"}]})))
    assert "lists no known allergies" in item["statement"]


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
    # "The records are silent" is false when one of them says something,
    # so the status record is not sent at all.
    assert not [i for i in out if i.get("not_an_allergy")]
    assert len(out) == 1


# --- a status record that would need hiding is not sent (#949 QA) ----------

@pytest.mark.parametrize("beside", ["real", "uncoded", "cut"])
def test_a_status_record_beside_an_allergy_or_in_a_cut_list_is_not_sent(
        beside):
    status = _allergy("s1", (SNOMED, STATUS))
    if beside == "real":
        out = _summarize_bundle(_bundle(status, _real()))
        assert len(out) == 1 and out[0]["name"] == "Penicillin"
    elif beside == "uncoded":
        uncoded = {"resourceType": "AllergyIntolerance", "id": "u",
                   "code": {"coding": [{"system": "urn:x", "code": "Z"}]}}
        out = _summarize_bundle(_bundle(status, uncoded))
        assert len(out) == 1 and out[0]["unreadable"] is True
    else:
        out = _summarize_bundle(_bundle(status, status, total=5))
        # Two status records dropped from five: three unseen, still cut.
        assert [i.get("truncated") for i in out] == [True]
    blob = json.dumps(out).lower()
    assert "not_an_allergy" not in blob
    assert "do not mention" not in blob and "not collected" not in blob


def test_dropped_status_records_are_not_counted():
    status = _allergy("s1", (SNOMED, STATUS))
    out = _summarize_bundle(_bundle(status, status, _real(), total=3))
    assert not any(i.get("truncated") for i in out)
    assert [i["name"] for i in out] == ["Penicillin"]


def test_a_status_only_list_keeps_its_plain_statement():
    status = _allergy("s1", (SNOMED, STATUS))
    out = _summarize_bundle(_bundle(status, status, total=2))
    assert [i["statement"] for i in out] == [
        "Your records don't say whether you have allergies."] * 2


@pytest.mark.parametrize("records", [
    lambda: [_allergy("s1", (SNOMED, STATUS)), _real()],
    lambda: [_nka(recordedDate="2020-01-02"), _real()],
    lambda: [_nka(verificationStatus="refuted")],
    lambda: [_allergy("s1", (SNOMED, STATUS)), _nka(recordedDate="2020-01-02")],
])
def test_no_note_in_any_allergy_answer_hides_anything(records):
    """Nothing is sent with an instruction to keep it from the person."""
    for total in (None, 9):
        blob = json.dumps(_summarize_bundle(_bundle(*records(), total=total)))
        assert "do not mention" not in blob.lower()


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


def test_real_engine_loinc_none_known_reaches_the_model_as_a_dated_entry(
        cfg, svc, monkeypatch):  # noqa: F811
    """The QA reproduction, through the real engine and its qualified text."""
    chain = Chain(cfg, svc, monkeypatch)
    _resolver(monkeypatch)
    _plant(chain, {"resourceType": "AllergyIntolerance", "id": "loinc-957",
                   "recordedDate": "2021-09-14",
                   "patient": {"reference": "Patient/demo-patient-rivera"},
                   "code": {"coding": [{"system": LOINC,
                                        "code": "LA10030-7"}]}})
    out, _system = _first_turn(chain, monkeypatch)
    [item] = out["allergies"]
    assert item["no_known_allergy_record"] is True
    assert item["statement"] == (
        "Your records include an entry, recorded on 2021-09-14, that lists "
        f"allergies as none known. {CONFIRM}")
    assert "Recorded entry" not in json.dumps(out)


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


# --- #949 F3: the concept outside AllergyIntolerance ------------------------

@pytest.mark.parametrize("rt", ["Condition", "Observation", "Procedure"])
@pytest.mark.parametrize("code", [NKA, NKDA, STATUS])
def test_an_absence_concept_on_any_type_is_never_its_name(rt, code):
    """Beside a real code, the record stays, unlabeled (fail-safe)."""
    res = {"resourceType": rt, "id": "x",
           "code": {"coding": [{"system": SNOMED, "code": code,
                                "display": "No known allergy"},
                               {"system": SNOMED, "code": "44054006"}],
                    "text": "No known allergy"}}
    [item] = _summarize_bundle(_bundle(res))
    assert item["name"] == "unlabeled record (SNOMED CT code)"
    assert "no known" not in json.dumps(item).lower()


@pytest.mark.parametrize("rt", ["Condition", "Observation", "Procedure"])
@pytest.mark.parametrize("codings", [
    [{"system": SNOMED, "code": NKA}],
    [{"system": "urn:oid:2.16.840.1.113883.6.96", "code": " " + NKDA}],
    [{"system": SNOMED, "code": NKA}, {"system": SNOMED, "code": STATUS}]])
def test_a_record_coded_only_as_an_absence_is_left_out(rt, codings):
    """No clinical finding: a "No known allergy" Condition read to people
    as a mystery illness (#949 patient tester). Left out of the list, and
    the list is not called longer for it."""
    res = {"resourceType": rt, "id": "x",
           "code": {"coding": codings, "text": "No known allergy"}}
    assert _summarize_bundle(_bundle(res)) == []
    assert _summarize_bundle(_bundle(res, total=1)) == []
    kept = {"resourceType": rt, "id": "y",
            "code": {"coding": [{"system": SNOMED, "code": "44054006"}]}}
    out = _summarize_bundle(_bundle(res, kept, total=2))
    assert [i.get("truncated") for i in out] == [None]


@pytest.mark.parametrize("other", [
    "junk", {"display": "x"}, {"system": SNOMED, "code": True}])
def test_an_absence_beside_anything_odd_is_kept_unlabeled(other):
    res = {"resourceType": "Condition", "id": "x",
           "code": {"coding": [{"system": SNOMED, "code": NKA}, other],
                    "text": "No known allergy"}}
    [item] = _summarize_bundle(_bundle(res))
    assert item["unreadable"] is True
    assert "no known" not in json.dumps(item).lower()


def test_an_unreadable_record_is_one_entry_to_ask_about_not_a_mystery():
    res = {"resourceType": "Observation", "id": "x",
           "effectiveDateTime": "2024-06-03",
           "code": {"coding": [{"system": SNOMED, "code": "999999999"}]}}
    [item] = _summarize_bundle(_bundle(res))
    note = item["note"].lower()
    assert "one entry from 2024-06-03 has no readable name" in note
    assert "their doctor can say what it is" in note
    assert "rule anything out" in note and "never say" in note
    undated = {"resourceType": "Condition", "id": "y",
               "code": {"coding": [{"system": "urn:x", "code": "Z"}]}}
    [item] = _summarize_bundle(_bundle(undated))
    assert "one entry has no readable name" in item["note"].lower()


@pytest.mark.parametrize("code", [NKA, "1003774007", STATUS])
@pytest.mark.parametrize("system", [SNOMED, "urn:oid:2.16.840.1.113883.6.96",
                                    " " + SNOMED])
def test_the_brief_never_names_a_record_by_an_absence_concept(
        monkeypatch, code, system):
    from r6.brief import engine
    monkeypatch.setattr("r6.terminology_resolver.resolve",
                        lambda s, c: "No known allergy")
    cond = {"resourceType": "Condition", "id": "c",
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": {"coding": [{"system": system, "code": code}]}}
    # Coded only as an absence: no finding, so not a condition at all.
    assert engine.build_problems([cond]) == []
    lab = {"resourceType": "Observation", "id": "o", "status": "final",
           "effectiveDateTime": "2024-01-02", "code": cond["code"]}
    assert engine.build_labs([lab]) == []
    # Beside a real code: kept, and never named by the absence label.
    mixed = dict(cond, code={"coding": [
        {"system": system, "code": code},
        {"system": SNOMED, "code": "44054006"}]})
    [field] = engine.build_problems([mixed])
    assert field.label == engine.UNLABELLED
    [field] = engine.build_labs([dict(lab, code=mixed["code"])])
    assert field.label == engine.UNLABELLED


def test_the_absence_sets_match_the_engine():
    """CareAgents imports nothing from the engine, so it mirrors the
    absence concepts; this holds the mirror equal to the engine's one
    source, r6.terminology (#950), set by set."""
    from careagents.agent import _ALLERGY_STATUS_CODES, _NKA_CODES
    from r6 import terminology
    assert set(_NKA_CODES) == (terminology.NKA_CONCEPTS
                               | terminology.LOINC_ABSENCE_ANSWERS)
    assert _ALLERGY_STATUS_CODES == terminology.ALLERGY_STATUS_CONCEPTS
    # Every absence code the engine writes qualified text for, in any
    # system: a code only the engine knows reached the model as an
    # ordinary allergy named by that text (#957 re-QA, LOINC LA10030-7).
    assert set(_NKA_CODES) | _ALLERGY_STATUS_CODES == (
        terminology.ABSENCE_CONCEPTS)


@pytest.mark.parametrize("code", [NKDA, "999999999"])
def test_a_coded_value_the_brief_cannot_name_is_not_called_missing(
        monkeypatch, code):
    """A value is on file: "Result not listed" said it was not (#949
    review). An absence concept and an unknown code read the same."""
    from r6.brief import engine
    monkeypatch.setattr("r6.terminology_resolver.resolve",
                        lambda s, c: "No known allergy")
    obs = {"resourceType": "Observation", "id": "v", "status": "final",
           "effectiveDateTime": "2024-01-02",
           "code": {"coding": [{"system": "http://loinc.org",
                                "code": "4548-4"}]},
           "valueCodeableConcept": {"coding": [{"system": SNOMED,
                                                "code": code}]}}
    if code != NKDA:
        monkeypatch.setattr("r6.terminology_resolver.resolve",
                            lambda s, c: None)
    [field] = engine.build_labs([obs])
    assert field.value.startswith(engine.UNLABELLED)
    assert engine.RESULT_NOT_LISTED not in field.value


def test_real_engine_brief_does_not_list_no_known_allergy_as_a_problem(
        cfg, svc, monkeypatch):  # noqa: F811
    from careagents.agent import _execute_tool
    from tests.test_beta_acceptance_rows import TENANT
    chain = Chain(cfg, svc, monkeypatch)
    _resolver(monkeypatch)
    _plant(chain, {"resourceType": "Condition", "id": "nka-946-brief",
                   "subject": {"reference": "Patient/demo-patient-rivera"},
                   "clinicalStatus": {"coding": [{"code": "active"}]},
                   "code": {"coding": [{"system": SNOMED, "code": NKA}]}})
    out = _execute_tool(chain.hc, TENANT, "appointment_brief", {}, [])
    assert "no known allergy" not in out.lower()
    # Left out, not a "Recorded, name unavailable" mystery condition
    # (#949 patient tester).
    assert "name unavailable" not in out.lower()
    problems = json.loads(out)["sections"]["problems"]
    assert {"label": "Type 2 diabetes mellitus", "value": "Active"} in (
        problems)


def test_real_engine_summary_leaves_out_a_no_known_allergy_condition(
        cfg, svc, monkeypatch):  # noqa: F811
    chain = Chain(cfg, svc, monkeypatch)
    _resolver(monkeypatch)
    _plant(chain, {"resourceType": "Condition", "id": "nka-946-cond",
                   "subject": {"reference": "Patient/demo-patient-rivera"},
                   "code": {"coding": [{"system": SNOMED, "code": NKA}]}})
    out, _system = _first_turn(chain, monkeypatch)
    blob = json.dumps(out).lower()
    assert "no known" not in blob
    assert "unlabeled" not in blob and "unreadable" not in blob
    assert not any(i.get("truncated") for i in out["conditions"])
    assert out["allergies"] == []
