"""A real allergy coded beside an absence code is a real allergy (FHI-48).

Real feeds send an AllergyIntolerance coded with a substance AND a "no
known allergy" concept (SNOMED 716186003), in either order. The engine
labels it by the substance alone (r6.terminology.label_codings, #957).
CareAgents blanked the name of any record that carried an absence code, so
the penicillin allergy reached the model as "unlabeled record (SNOMED CT
code)".

A record coded with a substance is now named by the substance's
terminology label, the `display` the engine wrote on that coding after
redaction. It is never named by the absence coding or by `code.text`, and it
never reads as "no known allergy". A record coded only with absence
concepts is unchanged: it is never named.

AGENTS.md: "No known allergies" is never inferred. Synthetic data only.
"""

from __future__ import annotations

import json
import logging

import pytest

from careagents.agent import ALLERGY_UNNAMED, _summarize_bundle
from tests.test_beta_acceptance_rows import Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_careagents_first_answer import _first_turn
from tests.test_sec_945_first_answer import (
    _audit_details, _free_text, _no_canary, _plant)

SNOMED = "http://snomed.info/sct"
OID = "urn:oid:2.16.840.1.113883.6.96"
NKA, NKDA, STATUS = "716186003", "409137002", "1631000175102"
PENICILLIN = "91936005"     # r6/terminology.py: "Allergy to penicillin"
CASHEW = "227493005"        # r6/terminology.py: "Cashew nut"
UNLABELLED = "999999999"    # no terminology label
#: A made-up patient name, planted where real feeds put one.
FAKE_NAME = "QZXCANARY Zelda Quornwhistle"

#: Fixture switch, never production: True makes the engine keep the
#: upstream `display` through redaction. A mutation run flips it to prove the
#: canary tests below catch a display that is not the terminology label.
KEEP_UPSTREAM_DISPLAY = False


def _bundle(*resources):
    return {"entry": [{"resource": r} for r in resources]}


def _mixed(rid, *codings, text=None):
    code = {"coding": list(codings)}
    if text is not None:
        code["text"] = text
    return {"resourceType": "AllergyIntolerance", "id": rid,
            "patient": {"reference": "Patient/demo-patient-rivera"},
            "code": code}


def _no_absence_words(item):
    blob = json.dumps(item).lower()
    assert "no known" not in blob, item
    assert "not asked" not in blob, item
    assert "recorded entry" not in blob, item


# --- the summary, as the engine hands it over --------------------------------
#
# After the engine: upstream text and displays are gone (redaction), the
# substance coding carries the terminology label, the absence coding none,
# and `code.text` is the substance label (label_codings, #957).

@pytest.mark.parametrize("order", ["substance-first", "absence-first"])
@pytest.mark.parametrize("absence", [
    {"system": SNOMED, "code": NKA}, {"system": OID, "code": NKA},
    {"system": SNOMED, "code": NKDA}, {"system": SNOMED, "code": STATUS}])
def test_a_substance_beside_an_absence_code_is_named_by_the_substance(
        order, absence):
    pen = {"system": SNOMED, "code": PENICILLIN,
           "display": "Allergy to penicillin"}
    codings = (pen, absence) if order == "substance-first" else (absence, pen)
    [item] = _summarize_bundle(_bundle(
        _mixed("m", *codings, text="Allergy to penicillin")))
    assert item["name"] == "Allergy to penicillin"
    assert "unreadable" not in item
    assert not item.get("no_known_allergy_record")
    assert not item.get("not_an_allergy")
    _no_absence_words(item)


def test_the_name_comes_from_the_substance_coding_never_code_text():
    """`code.text` is not trusted on a record carrying an absence code: an
    engine older than #957 wrote the absence label there."""
    [item] = _summarize_bundle(_bundle(_mixed(
        "m", {"system": SNOMED, "code": NKA},
        {"system": SNOMED, "code": PENICILLIN,
         "display": "Allergy to penicillin"}, text="No known allergy")))
    assert item["name"] == "Allergy to penicillin"
    _no_absence_words(item)


@pytest.mark.parametrize("absence", [
    {"system": SNOMED, "code": NKA, "display": "No known allergy"},
    {"system": SNOMED, "code": STATUS, "display": "Patient not asked"}])
def test_a_display_on_the_absence_coding_is_never_the_name(absence):
    [item] = _summarize_bundle(_bundle(_mixed(
        "m", absence,
        {"system": SNOMED, "code": CASHEW, "display": "Cashew nut"},
        text="No known allergy")))
    assert item["name"] == "Cashew nut"
    _no_absence_words(item)


def test_an_unlabelled_substance_beside_an_absence_code_stays_a_record():
    """No terminology label for the substance: the record is still a real
    allergy, said plainly, never dropped and never an absence."""
    [item] = _summarize_bundle(_bundle(_mixed(
        "m", {"system": SNOMED, "code": NKA},
        {"system": SNOMED, "code": UNLABELLED}, text="No known allergy")))
    assert item["name"] == ALLERGY_UNNAMED == "an allergy, name not recognised"
    assert item["unreadable"] is True
    assert "never absent" in item["note"]
    assert not item.get("no_known_allergy_record")


def test_a_record_coded_only_as_an_absence_is_still_never_named():
    """Unchanged: every coding an absence concept is a dated entry."""
    [item] = _summarize_bundle(_bundle(_mixed(
        "n", {"system": SNOMED, "code": NKA}, {"system": OID, "code": NKDA},
        text="No known allergy")))
    assert item["no_known_allergy_record"] is True
    assert "name" not in item


# --- end to end: real engine, real redaction, real labels --------------------

@pytest.mark.parametrize("order", ["substance-first", "absence-first"])
def test_real_feed_shape_reaches_the_model_named_by_its_substance(
        cfg, svc, monkeypatch, caplog, order):  # noqa: F811
    """A real-feed record (upstream display and text holding names, the OID
    spelling of SNOMED) goes through the engine's redaction and labels and
    reaches the model as a penicillin allergy, with no free text."""
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch)
    monkeypatch.setattr("r6.terminology_resolver.resolve",
                        lambda s, c: "No known allergy" if c == NKA else None)
    pen = {"system": SNOMED, "code": PENICILLIN}
    nka = {"system": OID, "code": NKA}
    codings = [pen, nka] if order == "substance-first" else [nka, pen]
    _plant(chain, {**_free_text(_mixed("fhi48-" + order, *codings), "a"),
                   "recordedDate": "2024-01-02"})

    out, _ = _first_turn(chain, monkeypatch)

    [item] = out["allergies"]
    assert item["name"] == "Allergy to penicillin"
    assert "unreadable" not in item
    _no_absence_words(item)
    _no_canary(json.dumps(out), caplog.text, _audit_details(chain))


# --- canaries: a name in the upstream display never reaches the model -------
#
# `_substance_label` reads `display`. That is safe only because the engine
# strips every upstream display and text and writes back nothing but the
# terminology label. These run through the real engine so they fail if that
# stops being true.

def _keep_upstream_display(monkeypatch):
    """The fixture-only mutation: redaction keeps every coding's display."""
    import r6.redaction as redaction
    original = redaction._redact_recursive

    def leaky(obj):
        kept = []

        def walk(o):
            if isinstance(o, dict):
                if isinstance(o.get("coding"), list):
                    kept.extend((c, c.get("display"))
                                for c in o["coding"] if isinstance(c, dict))
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(obj)
        original(obj)
        for coding, display in kept:
            if display is not None:
                coding["display"] = display
    monkeypatch.setattr(redaction, "_redact_recursive", leaky)


@pytest.mark.parametrize("order", ["substance-first", "absence-first"])
@pytest.mark.parametrize("substance,expected", [
    (PENICILLIN, "Allergy to penicillin"),   # (a) a terminology label
    (UNLABELLED, ALLERGY_UNNAMED),           # (b) no label
])
def test_a_name_in_the_upstream_display_never_reaches_the_model(
        cfg, svc, monkeypatch, caplog,  # noqa: F811
        order, substance, expected):
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch)
    if KEEP_UPSTREAM_DISPLAY:
        _keep_upstream_display(monkeypatch)
    monkeypatch.setattr("r6.terminology_resolver.resolve",
                        lambda s, c: "No known allergy" if c == NKA else None)
    sub = {"system": SNOMED, "code": substance, "display": FAKE_NAME}
    nka = {"system": OID, "code": NKA, "display": FAKE_NAME}
    codings = [sub, nka] if order == "substance-first" else [nka, sub]
    _plant(chain, {**_mixed("fhi48-c-%s-%s" % (order, substance), *codings,
                            text=FAKE_NAME), "recordedDate": "2024-01-02"})

    out, system = _first_turn(chain, monkeypatch)

    [item] = out["allergies"]
    assert item["name"] == expected
    _no_absence_words(item)
    _no_canary(json.dumps(out), system, caplog.text, _audit_details(chain))
