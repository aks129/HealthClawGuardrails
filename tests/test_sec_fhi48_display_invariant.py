"""Security tester: FHI-48's `_substance_label` reads `display`.

That is safe only while every display reaching CareAgents is the engine's
terminology label. These attack that invariant through the real engine:
upstream (proxy) mode, the opt-in resolver, nested and malformed codings,
records stored before redaction ran, and hostile code strings. Each plants a
canary where real feeds put a patient name and checks it never reaches the
model, the system prompt, the logs or the audit trail.

Also: an absence-only record is never named, and a mixed record is never a
"no known allergy" entry. Synthetic data only.
"""

from __future__ import annotations

import json
import logging

import pytest

from careagents.agent import ALLERGY_UNNAMED, _summarize_bundle
from tests.test_beta_acceptance_rows import Chain
from tests.test_careagents import cfg, svc  # noqa: F401  (pytest fixtures)
from tests.test_careagents_first_answer import _first_turn
from tests.test_sec_945_first_answer import _audit_details, _no_canary, _plant

SNOMED = "http://snomed.info/sct"
OID = "urn:oid:2.16.840.1.113883.6.96"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
NKA, NKDA, STATUS = "716186003", "409137002", "1631000175102"
PENICILLIN = "91936005"
UNLABELLED = "999999999"
NAME = "QZXCANARY Zelda Quornwhistle 555-0100"


def _allergy(rid, *codings, text=NAME, **extra):
    return {"resourceType": "AllergyIntolerance", "id": rid,
            "patient": {"reference": "Patient/demo-patient-rivera",
                        "display": NAME},
            "recordedDate": "2024-01-02",
            "code": {"coding": list(codings), "text": text}, **extra}


def _no_absence_words(item):
    blob = json.dumps(item).lower()
    assert "no known" not in blob and "not asked" not in blob, item


def _stub_resolver(monkeypatch, seen=None, answer=None):
    """The opt-in resolver, enabled. Records what it was asked."""
    def resolve(system, code):
        if seen is not None:
            seen.append((system, code))
        return (answer or {}).get(code)
    monkeypatch.setattr("r6.terminology_resolver.resolve", resolve)


def _run(chain, monkeypatch, caplog):
    out, system = _first_turn(chain, monkeypatch)
    _no_canary(json.dumps(out), system, caplog.text, _audit_details(chain))
    return out


# --- 1. nested concepts, reaction.substance, extensions, contained ----------

def test_nested_and_extension_displays_never_name_the_allergy(
        cfg, svc, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch)
    _stub_resolver(monkeypatch)
    nested = {"coding": [{"system": SNOMED, "code": UNLABELLED,
                          "display": NAME}], "text": NAME}
    res = _allergy(
        "x-nested",
        {"system": SNOMED, "code": NKA, "display": NAME,
         "extension": [{"url": "urn:x", "valueString": NAME,
                        "valueCodeableConcept": nested}]},
        {"system": SNOMED, "code": UNLABELLED, "display": NAME,
         "_display": {"extension": [{"url": "urn:x", "valueString": NAME}]}},
        reaction=[{"substance": nested, "manifestation": [nested],
                   "description": NAME}],
        extension=[{"url": "urn:x", "valueCodeableConcept": nested}],
        contained=[{"resourceType": "Substance", "id": "s", "code": nested}])
    _plant(chain, res)
    [item] = _run(chain, monkeypatch, caplog)["allergies"]
    assert item["name"] == ALLERGY_UNNAMED
    assert item["unreadable"] is True
    assert not item.get("no_known_allergy_record")


# --- 2. malformed codings ---------------------------------------------------

@pytest.mark.parametrize("bad", [
    NAME,                                                  # a string coding
    {"system": SNOMED, "code": [{"system": SNOMED, "code": PENICILLIN,
                                 "display": NAME}], "display": NAME},
    {"system": [NAME], "code": PENICILLIN, "display": NAME},
    {"system": SNOMED, "code": {"text": NAME}, "display": NAME},
    {"system": SNOMED, "code": f"91936005|{NAME}|", "display": NAME},
    {"system": SNOMED, "code": True, "display": NAME},
    {"system": "urn:x-local", "code": NAME, "display": NAME},
    {"system": SNOMED, "code": PENICILLIN, "display": {"value": NAME}},
    {"system": SNOMED, "code": PENICILLIN, "display": [NAME]},
])
def test_malformed_codings_beside_an_absence_never_leak(
        cfg, svc, monkeypatch, caplog, bad):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch)
    _stub_resolver(monkeypatch)
    _plant(chain, _allergy("x-bad", {"system": SNOMED, "code": NKA,
                                     "display": NAME}, bad))
    [item] = _run(chain, monkeypatch, caplog)["allergies"]
    # Never quoted as a "no known allergy" entry: something else is coded.
    # (A labelled penicillin coding may legitimately name it.)
    assert not item.get("no_known_allergy_record"), item
    _no_absence_words(item)
    assert item.get("name") in (ALLERGY_UNNAMED, "Allergy to penicillin"), item


# --- 3. the opt-in resolver --------------------------------------------------

def test_the_resolver_is_only_ever_handed_system_and_code(
        cfg, svc, monkeypatch, caplog):  # noqa: F811
    """Enabled resolver: it never sees the upstream display or text, and its
    label (keyed by code) is the only name used."""
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch)
    seen: list = []
    _stub_resolver(monkeypatch, seen, {"70618": "Penicillin G"})
    _plant(chain, _allergy("x-res", {"system": OID, "code": NKA,
                                     "display": NAME},
                           {"system": RXNORM, "code": "70618",
                            "display": NAME}))
    [item] = _run(chain, monkeypatch, caplog)["allergies"]
    assert item["name"] == "Penicillin G"
    assert all("QZXCANARY" not in json.dumps(a) for a in seen), seen
    # The absence code is never sent for a label (absence_text first).
    assert all(c != NKA for _, c in seen), seen


@pytest.mark.parametrize("label", ["No known allergy", "NKDA",
                                   "No allergy to penicillin"])
def test_a_resolver_absence_label_on_a_substance_is_never_the_name(
        cfg, svc, monkeypatch, caplog, label):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch)
    _stub_resolver(monkeypatch, None, {UNLABELLED: label})
    _plant(chain, _allergy("x-abs", {"system": SNOMED, "code": NKA},
                           {"system": SNOMED, "code": UNLABELLED}))
    [item] = _run(chain, monkeypatch, caplog)["allergies"]
    assert item["name"] == ALLERGY_UNNAMED
    _no_absence_words(item)


# --- 4. upstream (proxy) mode -----------------------------------------------

class _FakeProxy:
    def __init__(self, resources):
        self.resources = resources

    def search(self, resource_type, params):
        return ({"resourceType": "Bundle", "type": "searchset",
                 "entry": [{"resource": json.loads(json.dumps(r))}
                           for r in self.resources
                           if r["resourceType"] == resource_type]}, 200)

    def read(self, resource_type, resource_id):
        return None, 404


@pytest.mark.parametrize("order", ["substance-first", "absence-first"])
def test_upstream_mode_strips_the_display_before_careagents_reads_it(
        cfg, svc, monkeypatch, caplog, order):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch)
    _stub_resolver(monkeypatch)
    sub = {"system": SNOMED, "code": UNLABELLED, "display": NAME}
    nka = {"system": SNOMED, "code": NKA, "display": NAME}
    codings = [sub, nka] if order == "substance-first" else [nka, sub]
    proxy = _FakeProxy([_allergy("up-1", *codings),
                        _allergy("up-2", {"system": SNOMED, "code": NKA,
                                          "display": NAME})])
    monkeypatch.setattr("r6.routes.get_proxy_for_request", lambda: proxy)
    out = _run(chain, monkeypatch, caplog)
    allergies = out["allergies"]
    named = [a for a in allergies if not a.get("no_known_allergy_record")]
    assert [a["name"] for a in named] == [ALLERGY_UNNAMED]
    # The absence-only upstream record is a dated entry, never named.
    nka_items = [a for a in allergies if a.get("no_known_allergy_record")]
    assert all("name" not in a for a in nka_items)


# --- 5. stored before the redaction / #957 label change ---------------------

def test_a_record_stored_with_old_engine_labels_is_relabelled_on_read(
        cfg, svc, monkeypatch, caplog):  # noqa: F811
    """A row ingested earlier carries an old engine's words in display and
    text, and a canary on the substance. Read-time redaction strips both."""
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch)
    _stub_resolver(monkeypatch)
    _plant(chain, _allergy(
        "old-1", {"system": SNOMED, "code": NKA, "display": "No known allergy"},
        {"system": SNOMED, "code": PENICILLIN, "display": NAME},
        text="No known allergy"))
    [item] = _run(chain, monkeypatch, caplog)["allergies"]
    assert item["name"] == "Allergy to penicillin"
    _no_absence_words(item)


# --- 6. absence-only is never named; mixed is never "no known allergy" -------

@pytest.mark.parametrize("codings", [
    [{"system": SNOMED, "code": NKA, "display": NAME}],
    [{"system": OID, "code": int(NKA), "display": NAME}],
    [{"system": f" {SNOMED} ", "code": f" {NKA} ", "display": NAME}],
    [{"system": SNOMED, "code": NKA}, {"system": OID, "code": NKDA}],
    [{"system": "http://loinc.org", "code": "LA10030-7", "display": NAME}],
])
def test_absence_only_is_never_named(
        cfg, svc, monkeypatch, caplog, codings):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    chain = Chain(cfg, svc, monkeypatch)
    _stub_resolver(monkeypatch, None, {NKA: "Penicillin", NKDA: "Penicillin"})
    _plant(chain, _allergy("ab-1", *codings))
    [item] = _run(chain, monkeypatch, caplog)["allergies"]
    assert item.get("no_known_allergy_record") is True, item
    assert "name" not in item
    assert "penicillin" not in json.dumps(item).lower()


@pytest.mark.parametrize("absence", [NKA, NKDA, STATUS])
@pytest.mark.parametrize("substance", [PENICILLIN, UNLABELLED])
def test_mixed_is_never_a_no_known_allergy_entry_unit(absence, substance):
    """After-engine shape, every order: never NKA, never status, named only
    by the label or the plain fallback."""
    for codings in (
            [{"system": SNOMED, "code": absence},
             {"system": SNOMED, "code": substance}],
            [{"system": SNOMED, "code": substance},
             {"system": SNOMED, "code": absence}]):
        res = {"resourceType": "AllergyIntolerance", "id": "u",
               "code": {"coding": codings}}
        items = _summarize_bundle({"entry": [{"resource": res}]})
        assert len(items) == 1
        [item] = items
        assert not item.get("no_known_allergy_record")
        assert not item.get("not_an_allergy")
        _no_absence_words(item)
        assert item["name"] == ALLERGY_UNNAMED
