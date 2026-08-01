"""Server-derived code labels (#207).

The distinction this file exists to protect: a label may come from OUR table,
keyed by code, and never from the string an upstream system sent. An earlier
attempt at this fixed the readability problem by preserving `Coding.display`,
which leaked PHI — real feeds put patient names in that field. These tests pin
the safe version.
"""

from r6.redaction import apply_redaction
from r6.terminology import (label_codings, lookup, reset_unlabelled,
                            unlabelled_codes)

LOINC = "http://loinc.org"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
ICD9 = "http://hl7.org/fhir/sid/icd-9-cm"


def test_an_upstream_display_carrying_a_name_is_replaced_not_preserved():
    """The exact leak that killed the first attempt.

    Upstream sent "Glucose for Jane Secret" as the display of a standard LOINC
    coding. The label that survives must be the server's, with the name gone.
    """
    obs = {
        "resourceType": "Observation",
        "code": {"coding": [{"system": LOINC, "code": "2339-0",
                             "display": "Glucose for Jane Secret"}],
                 "text": "Glucose for Jane Secret"},
    }
    out = apply_redaction(obs)
    blob = str(out)

    assert "Jane" not in blob and "Secret" not in blob
    assert out["code"]["coding"][0]["display"] == "Glucose (blood)"
    assert out["code"]["text"] == "Glucose (blood)"


def test_an_unknown_code_is_left_unlabelled_rather_than_guessed():
    # "A record is here I could not read" beats a confident wrong name.
    obs = {"resourceType": "Observation",
           "code": {"coding": [{"system": LOINC, "code": "99999-9",
                                "display": "Something for Jane Secret"}]}}
    out = apply_redaction(obs)
    assert "display" not in out["code"]["coding"][0]
    assert "text" not in out["code"]
    assert "Jane" not in str(out)


def test_the_condition_that_was_reported_as_absent_now_reads():
    # #207: an active ICD-9 250.00 Condition reached the agent unlabelled and
    # it answered "no, you do not have diabetes".
    cond = {"resourceType": "Condition",
            "code": {"coding": [{"system": ICD9, "code": "250.00"}]}}
    out = apply_redaction(cond)
    assert "diabetes" in out["code"]["coding"][0]["display"].lower()


def test_labels_survive_the_whole_resource_tree():
    bundle = {"resourceType": "Bundle", "entry": [
        {"resource": {"resourceType": "Condition",
                      "code": {"coding": [{"system": ICD10, "code": "I10"}]}}}]}
    out = apply_redaction(bundle)
    coding = out["entry"][0]["resource"]["code"]["coding"][0]
    assert "blood pressure" in coding["display"].lower()


def test_oid_and_https_spellings_of_a_system_resolve():
    assert lookup("urn:oid:2.16.840.1.113883.6.1", "2339-0") == "Glucose (blood)"
    assert lookup("https://loinc.org", "2339-0") == "Glucose (blood)"


def test_medication_codings_are_labelled():
    med = {"resourceType": "MedicationRequest",
           "medicationCodeableConcept": {"coding": [{
               "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
               "code": "860975"}]}}
    out = apply_redaction(med)
    assert "Metformin" in out["medicationCodeableConcept"]["coding"][0]["display"]


def test_misses_are_recorded_so_the_map_grows_from_evidence():
    reset_unlabelled()
    label_codings({"coding": [{"system": LOINC, "code": "12345-6"}]})
    label_codings({"coding": [{"system": LOINC, "code": "12345-6"}]})
    top = dict(unlabelled_codes())
    assert top[(LOINC, "12345-6")] == 2
    reset_unlabelled()


def test_labelling_never_invents_a_field_on_an_empty_concept():
    out = apply_redaction({"resourceType": "Observation", "code": {}})
    assert out["code"] == {}


def test_every_code_the_live_demo_tenant_serves_has_a_label():
    """Measured from the deployed demo tenant on 2026-08-01.

    These are what the HIMSS demo actually shows; if one loses its label the
    demo silently regresses to "unlabeled record, code ...".
    """
    live = [(LOINC, "2339-0"), (LOINC, "4548-4"), (LOINC, "55284-4"),
            (LOINC, "2823-3"), (LOINC, "85354-9"), (LOINC, "13457-7"),
            (ICD9, "250.00"), (ICD10, "E11.9"), (ICD10, "I10"),
            ("http://www.nlm.nih.gov/research/umls/rxnorm", "860975")]
    missing = [c for c in live if not lookup(*c)]
    assert not missing, f"demo codes with no label: {missing}"
