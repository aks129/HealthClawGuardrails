"""medicationReference chasing in the agent's bundle summary.

Live finding, 2026-08-04 (MEDENT tenant): all four MedicationRequest rows
carry NO inline code — no `medicationCodeableConcept` at all. The name lives
on a referenced Medication resource, which does carry a proper coding. The
agent told the patient "I can't read the names of these medications" while
four correctly-coded Medication rows sat in their record.

These tests stub the HealthClaw client (the repo-wide caveat applies: they
prove the read is MADE and its result USED, not that a live server accepts
it — the live check is shakeout row S3).
"""
from __future__ import annotations

from careagents.agent import (MAX_MEDICATION_DEREFS, _medication_resolver,
                              _summarize_bundle)
from careagents.healthclaw import HealthClawError


def _med_request(rid, ref=None, code=None, status="active"):
    res = {"resourceType": "MedicationRequest", "id": rid, "status": status}
    if ref:
        res["medicationReference"] = {"reference": ref}
    if code:
        res["medicationCodeableConcept"] = code
    return {"resource": res}


class _StubHC:
    """Serves Medication reads from a dict; counts every read it makes."""

    def __init__(self, medications: dict, fail: bool = False):
        self.medications = medications
        self.fail = fail
        self.reads: list[str] = []

    def read(self, tenant, resource_type, resource_id):
        self.reads.append(f"{resource_type}/{resource_id}")
        if self.fail:
            raise HealthClawError("read failed (503)", 503)
        med = self.medications.get(resource_id)
        if med is None:
            raise HealthClawError("read failed (404)", 404)
        return med


def _resolver(hc):
    return _medication_resolver(hc, "t")


def test_the_name_is_recovered_from_the_referenced_medication():
    """MUTATION: drop the resolve_ref branch in _summarize_bundle -> red.

    This is the live MEDENT shape: request carries only a reference; the
    Medication carries the (server-labelled) coding.
    """
    hc = _StubHC({"m1": {"resourceType": "Medication", "id": "m1",
                         "code": {"text": "Atorvastatin 20 MG Oral Tablet"}}})
    items = _summarize_bundle(
        {"entry": [_med_request("r1", ref="Medication/m1")]},
        resolve_ref=_resolver(hc))
    assert items[0]["name"] == "Atorvastatin 20 MG Oral Tablet"
    assert "unreadable" not in items[0]
    assert hc.reads == ["Medication/m1"]


def test_coding_display_is_used_when_text_is_absent():
    hc = _StubHC({"m1": {"resourceType": "Medication", "code": {
        "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                    "code": "617312", "display": "Atorvastatin 20 MG"}]}}})
    items = _summarize_bundle(
        {"entry": [_med_request("r1", ref="Medication/m1")]},
        resolve_ref=_resolver(hc))
    assert items[0]["name"] == "Atorvastatin 20 MG"


def test_an_inline_code_wins_and_makes_no_read():
    """The deref is a fallback, not a replacement for the inline path."""
    hc = _StubHC({})
    items = _summarize_bundle(
        {"entry": [_med_request("r1", ref="Medication/m1",
                                code={"text": "Lisinopril 10 MG"})]},
        resolve_ref=_resolver(hc))
    assert items[0]["name"] == "Lisinopril 10 MG"
    assert hc.reads == []


def test_a_failed_read_stays_unreadable_never_absent():
    """MUTATION: drop the item on failure -> red. #207's rule, again:
    a record whose name cannot be fetched is still a record."""
    hc = _StubHC({}, fail=True)
    items = _summarize_bundle(
        {"entry": [_med_request("r1", ref="Medication/m1")]},
        resolve_ref=_resolver(hc))
    assert len(items) == 1
    assert items[0]["unreadable"] is True
    assert items[0]["status"] == "active"


def test_an_unlabelled_medication_stays_unreadable():
    """The referenced Medication exists but its coding survived with no
    display (the label table missed it). Same rule: present, unreadable."""
    hc = _StubHC({"m1": {"resourceType": "Medication", "code": {
        "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                    "code": "999999"}]}}})
    items = _summarize_bundle(
        {"entry": [_med_request("r1", ref="Medication/m1")]},
        resolve_ref=_resolver(hc))
    assert items[0]["unreadable"] is True


def test_one_reference_costs_one_read():
    """MUTATION: drop the memo -> red. Four requests to the same drug must
    not become four audited reads per chat message."""
    hc = _StubHC({"m1": {"resourceType": "Medication",
                         "code": {"text": "Atorvastatin 20 MG"}}})
    bundle = {"entry": [_med_request(f"r{i}", ref="Medication/m1")
                        for i in range(4)]}
    items = _summarize_bundle(bundle, resolve_ref=_resolver(hc))
    assert [i["name"] for i in items] == ["Atorvastatin 20 MG"] * 4
    assert hc.reads == ["Medication/m1"]


def test_the_fan_out_is_capped():
    """MUTATION: remove MAX_MEDICATION_DEREFS -> red."""
    meds = {f"m{i}": {"resourceType": "Medication",
                      "code": {"text": f"Drug {i}"}}
            for i in range(MAX_MEDICATION_DEREFS + 5)}
    hc = _StubHC(meds)
    bundle = {"entry": [_med_request(f"r{i}", ref=f"Medication/m{i}")
                        for i in range(MAX_MEDICATION_DEREFS + 5)]}
    _summarize_bundle(bundle, limit=MAX_MEDICATION_DEREFS + 5,
                      resolve_ref=_resolver(hc))
    assert len(hc.reads) == MAX_MEDICATION_DEREFS


def test_a_failed_read_is_memoised_too():
    """A dead reference must not be re-fetched for every row that shares it."""
    hc = _StubHC({}, fail=True)
    bundle = {"entry": [_med_request(f"r{i}", ref="Medication/m1")
                        for i in range(3)]}
    _summarize_bundle(bundle, resolve_ref=_resolver(hc))
    assert hc.reads == ["Medication/m1"]


def test_only_medication_references_are_chased():
    """MUTATION: chase any reference type -> red. The cap and the audit story
    are argued for Medication only; a Patient/ ref must never be fetched."""
    hc = _StubHC({})
    resolve = _resolver(hc)
    # resolve now returns (label, reason) so the caller can tell "we could not
    # look" from "we looked and there is no name" (2026-08-05). A non-
    # Medication ref is neither: it is a ref we decline to chase at all.
    for ref in ("Patient/p1", "Observation/o1", None,
                {"reference": "Medication/m1"}):
        assert resolve(ref) == (None, "not-a-ref"), ref
    assert hc.reads == []


# ---------------------------------------------------------------------------
# "Could not look it up" is not "the source sent free text" (2026-08-05).
#
# PR #376 fixed a case where four correctly-coded medications were reported
# unreadable, and the agent volunteered a confident FALSE explanation to the
# patient: "The source system sent these as free-text notes rather than
# standardized codes." That sentence came from the note below, which is a
# claim about the upstream feed made by a branch that never learned anything
# about the upstream feed.
#
# #376 closed the RxNorm cause. The same false sentence is still reachable
# from three others, because `resolve_ref(ref) or ""` collapses them all:
#
#   - the deref cap (MAX_MEDICATION_DEREFS) was hit — we never looked
#   - the Medication read failed — we looked and could not tell
#   - the Medication genuinely carries no label — the only case the sentence
#     is true for
#
# A patient on more than ten medications gets the false sentence for the
# overflow. A patient during a backend blip gets it for everything.
# ---------------------------------------------------------------------------
def _names(items):
    return [i.get("name") for i in items]


def test_a_read_failure_never_claims_the_source_sent_free_text():
    hc = _StubHC({}, fail=True)
    resolver = _medication_resolver(hc, "t1")

    items = _summarize_bundle(
        {"entry": [_med_request("mr1", ref="Medication/m1")]},
        limit=10, resolve_ref=resolver)

    assert items[0]["unreadable"] is True, "unreadable-not-absent still holds"
    assert not items[0].get("uncoded"), (
        "a failed read was reported as a fact about the source feed")
    assert "not coded at the source" not in (items[0].get("name") or "")
    assert "free text" not in (items[0].get("note") or "").lower()


def test_the_deref_cap_never_claims_the_source_sent_free_text():
    """The overflow rows were never looked at. Saying anything about how the
    source coded them is inventing a finding."""
    meds = {f"m{i}": {"resourceType": "Medication", "id": f"m{i}",
                      "code": {"text": f"Drug {i}"}}
            for i in range(MAX_MEDICATION_DEREFS + 3)}
    hc = _StubHC(meds)
    resolver = _medication_resolver(hc, "t1")
    entries = [_med_request(f"mr{i}", ref=f"Medication/m{i}")
               for i in range(MAX_MEDICATION_DEREFS + 3)]

    items = _summarize_bundle({"entry": entries}, limit=50,
                              resolve_ref=resolver)

    overflow = [i for i in items if not i.get("name", "").startswith("Drug")]
    assert overflow, "the cap did not engage; this test proves nothing"
    for item in overflow:
        assert not item.get("uncoded"), (
            "a row we never read was reported as uncoded at the source")


def test_a_medication_with_no_label_still_earns_the_source_sentence():
    """The one case the sentence is true for must keep it — otherwise this
    fix trades a false explanation for no explanation."""
    hc = _StubHC({"m1": {"resourceType": "Medication", "id": "m1",
                         "code": {}}})
    resolver = _medication_resolver(hc, "t1")

    items = _summarize_bundle(
        {"entry": [_med_request("mr1", ref="Medication/m1")]},
        limit=10, resolve_ref=resolver)

    assert items[0]["uncoded"] is True
    assert items[0]["name"] == "recorded but not coded at the source"


def test_a_contained_or_uuid_reference_never_claims_the_source_sent_free_text():
    """The fourth route to the false sentence, missed by #379.

    `resolve` returns "not-a-ref" for any reference that is not
    `Medication/<id>` — which includes `#contained` and `urn:uuid:` targets.
    Both are ordinary FHIR: a contained Medication and a bundle-local one.

    We decline to chase those, so we learn nothing about how the source coded
    them — exactly the state "unavailable" and "not-attempted" describe. They
    were routed to the source-blame wording instead, which asserts a finding
    about the upstream feed from a branch that never looked.

    A MedicationRequest with an INLINE uncoded concept and no reference at all
    is different: there `lookup_reason` is None, we did see the concept, and
    it genuinely had no code. That case keeps the source sentence, and
    test_a_medication_with_no_label_still_earns_the_source_sentence guards it.
    """
    hc = _StubHC({})
    resolver = _medication_resolver(hc, "t1")

    for ref in ("#med-1", "urn:uuid:1e2d3c4b-0000-0000-0000-000000000001"):
        items = _summarize_bundle(
            {"entry": [_med_request("mr1", ref=ref)]},
            limit=10, resolve_ref=resolver)
        assert items[0]["unreadable"] is True, ref
        assert not items[0].get("uncoded"), (
            f"{ref} was reported as uncoded at the source, but we never "
            "looked at it")
    assert hc.reads == [], "a non-Medication reference was fetched"
