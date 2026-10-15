"""Security review of PR #957 (#950, FHI-43): absence-concept bypasses.

PR #957 claims an absence or allergy-status concept never reaches a
consumer as a bare name. Its guard (`terminology.is_absence_coding`) keys on
an exact (system, code) string in a closed set of eight codes. The resolver
that writes labels (`r6/terminology_resolver.resolve`, on in production per
docs/evidence/2026-10-01-synthetic-hospital-shakeout.md) forwards the same
code string to tx.fhir.org, which answers for more spellings and more
concepts than the set holds. Every code below was checked live against
tx.fhir.org on 2026-10-14 and returned the display recorded in
`_TX_FHIR_ORG`; the fixture replays those answers so CI never touches the
network:

- 160244002 is the inactive predecessor of 716186003 ("No known
  allergies", retired 2016-07-31). It is not a descendant, so the isa
  list in the PR misses it, and legacy feeds still carry it.
- "0716186003" (a leading zero), "716186003|No known allergy|" (SNOMED
  compositional grammar with its term) and "716186003+716186003" (an
  expression) all resolve; none is in the set.

These failed on dd04203. They are now closed in three layers
(r6/terminology.py): a SNOMED code must be a well-formed concept id before
it is looked up or sent to the resolver; 160244002 is in NKA_CONCEPTS; and
a resolver answer that reads as an absence or status statement is never
used as a label. The last block holds controls that held already (audit
detail, share bundle, the SDC attestation item, the alias and int
spellings).

Synthetic data only. "Zelda Quornwhistle" is an invented canary name.
"""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock, patch

import pytest

from r6 import terminology
from r6.redaction import apply_patient_controlled_redaction, apply_redaction
from r6.terminology import SNOMED, label_codings

CANARY = "Zelda Quornwhistle"

#: Live tx.fhir.org $validate-code displays, captured 2026-10-14.
_TX_FHIR_ORG = {
    "716186003": "No known allergy",
    "409137002": "No known drug allergy",
    "1631000175102": "Patient not asked",
    "160244002": "No known allergies",
    "0716186003": "No known allergy",
    "716186003|No known allergy|": "No known allergy",
    "716186003+716186003":
        "716186003|No known allergy|+716186003|No known allergy|",
    "91936005": "Allergy to penicillin",
}

LEGACY_NKA = "160244002"
VARIANTS = ("0716186003", "716186003|No known allergy|",
            "716186003+716186003")
BYPASSES = (LEGACY_NKA,) + VARIANTS

#: A bare absence name: "no known ..." or "not asked", outside our own
#: qualified sentence (which quotes it after "Recorded entry: '").
_BARE = re.compile(r"(?<!Recorded entry: ')(no known allerg|patient not asked)",
                   re.IGNORECASE)


@pytest.fixture(autouse=True)
def _tx_fhir_org(monkeypatch):
    """Production's resolver, replayed from the live answers above."""
    monkeypatch.setattr(
        "r6.terminology_resolver.resolve",
        lambda system, code: _TX_FHIR_ORG.get(code)
        if system == SNOMED else None)


def _assert_no_bare_name(obj):
    blob = obj if isinstance(obj, str) else json.dumps(obj)
    hits = _BARE.findall(blob)
    assert not hits, f"bare absence name reached the consumer: {blob[:600]}"


def _allergy(codes, rid="sec957", system=SNOMED):
    return {
        "resourceType": "AllergyIntolerance", "id": rid,
        "patient": {"reference": "Patient/pt-957"},
        "code": {"coding": [{"system": system, "code": c,
                             "display": f"NKA - {CANARY}"} for c in codes],
                 "text": f"{CANARY} reports nothing"},
    }


# --- F1: the inactive legacy concept 160244002 ------------------------------

def test_f1_legacy_nka_concept_gets_qualified_text_not_a_name():
    concept = {"coding": [{"system": SNOMED, "code": LEGACY_NKA}]}
    label_codings(concept)
    _assert_no_bare_name(concept)
    assert "display" not in concept["coding"][0]
    assert concept["text"] == terminology.absence_text(SNOMED, LEGACY_NKA)


def test_f1_legacy_nka_beside_a_real_absence_concept():
    """[716186003, 160244002]: at dd04203 the second coding supplied
    display and text as "No known allergies". Both are absences now."""
    out = apply_redaction(_allergy(["716186003", LEGACY_NKA]))
    assert CANARY not in json.dumps(out)
    _assert_no_bare_name(out)
    assert all("display" not in c for c in out["code"]["coding"])
    assert out["code"]["text"] == terminology.absence_text(SNOMED, "716186003")


# --- F2: code spellings the resolver accepts and the guard does not ---------

@pytest.mark.parametrize("code", VARIANTS)
def test_f2_code_variant_gets_no_label_at_all(code):
    out = apply_redaction(_allergy([code]))
    assert CANARY not in json.dumps(out)
    _assert_no_bare_name(out)
    assert "display" not in out["code"]["coding"][0]
    assert "text" not in out["code"]


@pytest.mark.parametrize("code", BYPASSES)
def test_guard_and_resolver_agree_on_what_is_an_absence(code):
    """The invariant the fix needs: any code the resolver names as an
    absence is one the guard recognises."""
    label = terminology.lookup(SNOMED, code)
    if label and _BARE.search(label):
        assert terminology.is_absence_coding(
            {"system": SNOMED, "code": code}), (code, label)


# --- every exit: local and upstream, read and search ------------------------

def _plant(app, tenant_id, resource):
    from models import db
    from r6.models import R6Resource
    with app.app_context():
        db.session.add(R6Resource(resource["resourceType"],
                                  json.dumps(resource),
                                  resource_id=resource["id"],
                                  tenant_id=tenant_id))
        db.session.commit()


@pytest.mark.parametrize("code", BYPASSES)
def test_local_read_and_search_name_no_absence(app, client, tenant_id,
                                               tenant_headers, code):
    _plant(app, tenant_id, _allergy([code]))
    read = client.get("/r6/fhir/AllergyIntolerance/sec957",
                      headers=tenant_headers)
    search = client.get("/r6/fhir/AllergyIntolerance", headers=tenant_headers)
    assert read.status_code == 200 and search.status_code == 200
    for resp in (read, search):
        assert CANARY not in resp.get_data(as_text=True)
        _assert_no_bare_name(resp.get_data(as_text=True))


@pytest.mark.parametrize("code", BYPASSES)
def test_upstream_read_and_search_name_no_absence(client, tenant_headers,
                                                  code):
    proxy = MagicMock()
    proxy.read.return_value = (_allergy([code]), 200)
    proxy.search.return_value = ({
        "resourceType": "Bundle", "type": "searchset", "total": 1,
        "entry": [{"resource": _allergy([code])}]}, 200)
    with patch("r6.routes.get_proxy_for_request", return_value=proxy):
        read = client.get("/r6/fhir/AllergyIntolerance/sec957",
                          headers=tenant_headers)
        search = client.get("/r6/fhir/AllergyIntolerance",
                            headers=tenant_headers)
    assert read.status_code == 200 and search.status_code == 200
    for resp in (read, search):
        assert CANARY not in resp.get_data(as_text=True)
        _assert_no_bare_name(resp.get_data(as_text=True))


# --- SDC prefill and the brief ----------------------------------------------

def _walk(items):
    for item in items:
        yield item
        yield from _walk(item.get("item", []))
        for answer in item.get("answer", []):
            yield from _walk(answer.get("item", []))


@pytest.mark.parametrize("code", BYPASSES)
def test_intake_prefill_never_answers_an_allergen_as_no_known_allergies(code):
    from r6.sdc.intake import intake_questionnaire
    from r6.sdc.populate import populate_questionnaire

    qr, _ = populate_questionnaire(
        intake_questionnaire(), {"resourceType": "Patient", "id": "pt-957"},
        [apply_redaction(_allergy([code]))])
    items = list(_walk(qr["item"]))
    [attest] = [i for i in items
                if i["linkId"] == "allergies.no-known-allergies"]
    assert "answer" not in attest  # holds today
    allergens = [a.get("valueString") for i in items
                 if i["linkId"] == "allergies.item.allergen"
                 for a in i.get("answer", [])]
    _assert_no_bare_name(allergens)


@pytest.mark.parametrize("code", BYPASSES)
def test_brief_problem_list_names_no_absence(code):
    from r6.brief import engine
    condition = {
        "resourceType": "Condition", "id": "c957",
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"coding": [{"system": SNOMED, "code": code}]},
    }
    fields = engine.build_problems([apply_redaction(condition)])
    _assert_no_bare_name([f.label for f in fields])


# --- controls that already hold ---------------------------------------------

@pytest.mark.parametrize("system", (SNOMED, "urn:oid:2.16.840.1.113883.6.96",
                                    f"\t{SNOMED} "))
@pytest.mark.parametrize("code", ("716186003", 716186003, " 716186003\n",
                                  "1631000175102"))
def test_control_listed_codes_in_every_spelling_hold(system, code):
    out = apply_redaction(_allergy([code], system=system))
    assert CANARY not in json.dumps(out)
    _assert_no_bare_name(out)


@pytest.mark.parametrize("codes", (["716186003", "91936005"],
                                   ["91936005", "716186003"]))
def test_control_mixed_with_a_real_allergen_both_orders(codes):
    out = apply_redaction(_allergy(codes))
    assert out["code"]["text"] == "Allergy to penicillin"
    _assert_no_bare_name(out)


def test_control_share_bundle_writes_no_label():
    out = apply_patient_controlled_redaction(
        _allergy(["716186003", LEGACY_NKA]), "pt-957")
    assert CANARY not in json.dumps(out)
    _assert_no_bare_name(out)


def test_control_audit_detail_is_free_of_absence_text(app, client, tenant_id,
                                                      tenant_headers):
    from r6.models import AuditEventRecord
    _plant(app, tenant_id, _allergy(["716186003"]))
    client.get("/r6/fhir/AllergyIntolerance/sec957", headers=tenant_headers)
    client.get("/r6/fhir/AllergyIntolerance", headers=tenant_headers)
    with app.app_context():
        rows = AuditEventRecord.query.filter_by(tenant_id=tenant_id).all()
        assert rows
        blob = json.dumps([r.to_dict() if hasattr(r, "to_dict") else
                           {k: str(v) for k, v in vars(r).items()
                            if not k.startswith("_")} for r in rows])
    assert CANARY not in blob
    assert "Recorded entry" not in blob
    _assert_no_bare_name(blob)
