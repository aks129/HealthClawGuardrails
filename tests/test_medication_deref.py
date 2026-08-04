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
    assert resolve("Patient/p1") is None
    assert resolve("Observation/o1") is None
    assert resolve(None) is None
    assert resolve({"reference": "Medication/m1"}) is None
    assert hc.reads == []
