import json

from r6.sdc.extract import extract_resources

OBS_EXTRACT_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-observationExtract"
)
DEF_EXTRACT_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-definitionExtract"
)


def test_extract_observation_based():
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{
             "linkId": "weight",
             "type": "quantity",
             "code": [{"system": "http://loinc.org", "code": "29463-7"}],
             "extension": [{"url": OBS_EXTRACT_URL, "valueBoolean": True}],
         }]}
    qr = {"resourceType": "QuestionnaireResponse", "status": "completed",
          "subject": {"reference": "Patient/p1"},
          "item": [{"linkId": "weight",
                    "answer": [{"valueQuantity": {"value": 70, "unit": "kg"}}]}]}

    bundle = extract_resources(qr, q)

    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "transaction"
    obs = bundle["entry"][0]["resource"]
    assert obs["resourceType"] == "Observation"
    assert obs["code"]["coding"][0]["code"] == "29463-7"
    assert obs["valueQuantity"]["value"] == 70
    assert obs["subject"] == {"reference": "Patient/p1"}
    assert bundle["entry"][0]["request"]["method"] == "POST"


def test_extract_definition_based():
    q = {"resourceType": "Questionnaire", "status": "active",
         "extension": [{"url": DEF_EXTRACT_URL,
                        "valueCode": "Patient"}],
         "item": [
             {"linkId": "family", "type": "string",
              "definition": "http://hl7.org/fhir/StructureDefinition/"
                            "Patient#Patient.name.family"},
             {"linkId": "dob", "type": "date",
              "definition": "http://hl7.org/fhir/StructureDefinition/"
                            "Patient#Patient.birthDate"},
         ]}
    qr = {"resourceType": "QuestionnaireResponse", "status": "completed",
          "item": [
              {"linkId": "family", "answer": [{"valueString": "Lovelace"}]},
              {"linkId": "dob", "answer": [{"valueDate": "1815-12-10"}]},
          ]}

    bundle = extract_resources(qr, q)

    patient = bundle["entry"][0]["resource"]
    assert patient["resourceType"] == "Patient"
    assert patient["name"][0]["family"] == "Lovelace"
    assert patient["birthDate"] == "1815-12-10"


def test_extract_empty_when_no_directives():
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{"linkId": "x", "type": "string"}]}
    qr = {"resourceType": "QuestionnaireResponse", "status": "completed",
          "item": [{"linkId": "x", "answer": [{"valueString": "y"}]}]}

    bundle = extract_resources(qr, q)
    assert bundle["entry"] == []


# ---------------------------------------------------------------------------
# #572, part 1: an answer whose definition names ANOTHER resource type is never
# written into the extraction target.
# ---------------------------------------------------------------------------

def _intake_like_questionnaire():
    return {
        "resourceType": "Questionnaire",
        "extension": [{"url": DEF_EXTRACT_URL, "valueCode": "Patient"}],
        "item": [
            {"linkId": "family", "type": "string",
             "definition": "http://hl7.org/fhir/StructureDefinition/Patient#Patient.name.family"},
            {"linkId": "allergies", "type": "group", "item": [
                {"linkId": "allergies.item.allergen", "type": "string",
                 "definition": "http://hl7.org/fhir/StructureDefinition/"
                               "AllergyIntolerance#AllergyIntolerance.code.text"},
            ]},
        ],
    }


def _intake_like_response():
    return {
        # No subject on purpose: part 2 of #572 stops a subject-bound response
        # from yielding a Patient at all, and these pins are about the
        # allergen answer, not the subject.
        "resourceType": "QuestionnaireResponse", "status": "completed",
        "item": [
            {"linkId": "family", "answer": [{"valueString": "Synthetic"}]},
            {"linkId": "allergies", "item": [
                {"linkId": "allergies.item.allergen",
                 "answer": [{"valueString": "peanut-572"}]},
            ]},
        ],
    }


def test_an_answer_for_another_resource_type_never_lands_on_the_target(caplog):
    """The engine walked every `definition` into the one target type, so the
    allergen answer (AllergyIntolerance#AllergyIntolerance.code.text) was
    written as Patient.code.text: an element Patient does not have, holding
    clinical text no clinician can find and no validator catches (#572).

    MUTATION: r6/sdc/extract.py, drop the resource-type comparison in
    _extract_by_definition -> red (the Patient carries `code`).
    """
    import json
    import logging
    with caplog.at_level(logging.WARNING, logger="r6.sdc.extract"):
        bundle = extract_resources(_intake_like_response(), _intake_like_questionnaire())
    patients = [e["resource"] for e in bundle["entry"]
                if e["resource"]["resourceType"] == "Patient"]
    assert len(patients) == 1
    assert patients[0]["name"][0]["family"] == "Synthetic"   # demographics still extract
    assert "code" not in patients[0]
    assert "peanut-572" not in json.dumps(bundle)              # not written anywhere
    # The drop is said out loud, naming the item and the two types, never
    # the answer.
    dropped = [r for r in caplog.records if "not extracted" in r.getMessage()]
    assert len(dropped) == 1
    msg = dropped[0].getMessage()
    assert "allergies.item.allergen" in msg
    assert "AllergyIntolerance" in msg and "Patient" in msg
    assert "peanut-572" not in msg


def test_a_definition_for_the_target_type_still_extracts():
    q = _intake_like_questionnaire()
    qr = _intake_like_response()
    bundle = extract_resources(qr, q)
    assert [e["resource"]["resourceType"] for e in bundle["entry"]] == ["Patient"]


def test_a_definition_whose_url_and_path_disagree_is_refused(caplog):
    """The QA pass on #664: `AllergyIntolerance#Patient.name.given` names one
    type in the URL and another in the path. A path-only check let the
    allergen answer land in Patient.name.given. Both halves must be the
    target type now.

    MUTATION: r6/sdc/extract.py, drop the URL half of the comparison -> red
    (the Patient carries name.given).
    """
    import json
    import logging
    q = _intake_like_questionnaire()
    q["item"][1]["item"][0]["definition"] = (
        "http://hl7.org/fhir/StructureDefinition/AllergyIntolerance#Patient.name.given")
    with caplog.at_level(logging.WARNING, logger="r6.sdc.extract"):
        bundle = extract_resources(_intake_like_response(), q)
    patients = [e["resource"] for e in bundle["entry"]
                if e["resource"]["resourceType"] == "Patient"]
    assert len(patients) == 1
    assert patients[0]["name"][0].get("given") is None
    assert "peanut-572" not in json.dumps(bundle)
    dropped = [r.getMessage() for r in caplog.records if "not extracted" in r.getMessage()]
    assert len(dropped) == 1
    assert "AllergyIntolerance" in dropped[0] and "Patient" in dropped[0]


def test_bare_absent_and_profile_url_forms_of_the_target_type_still_extract():
    """A custom profile URL does not carry its base type in its name
    (us-core-patient, http://example.org/SD), so it cannot be checked
    without resolving the profile; the element path's type is what governs
    there, as it always did. Only the HL7 base namespace and a bare type
    name are read off the URL."""
    for definition in ("Patient#Patient.name.family", "#Patient.name.family",
                       "http://example.org/fhir/StructureDefinition/my-patient#Patient.name.family",
                       "http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient#Patient.name.family"):
        q = _intake_like_questionnaire()
        q["item"][0]["definition"] = definition
        bundle = extract_resources(_intake_like_response(), q)
        patient = bundle["entry"][0]["resource"]
        assert patient["resourceType"] == "Patient"
        assert patient["name"][0]["family"] == "Synthetic", definition


def test_a_definition_with_no_element_path_does_not_crash():
    q = _intake_like_questionnaire()
    q["item"][0]["definition"] = "http://hl7.org/fhir/StructureDefinition/Patient#"
    bundle = extract_resources(_intake_like_response(), q)
    assert all(e["resource"]["resourceType"] == "Patient" for e in bundle["entry"])



# ---------------------------------------------------------------------------
# #572 part 2A: a response bound to a subject never yields a Patient entry.
# Each committed form used to create a NEW Patient with a fresh uuid, so a
# tenant accumulated a Patient per submission and every downstream check
# lost its subject. The review page renders demographics read-only, so no
# human-confirmed demographic change exists to write back; a subject-less
# response still creates one, as it always did.
# ---------------------------------------------------------------------------

def _subject_bound(qr):
    qr = json.loads(json.dumps(qr)) if isinstance(qr, dict) else qr
    qr["subject"] = {"reference": "Patient/p-572"}
    return qr


def test_a_subject_bound_response_never_yields_a_patient():
    """MUTATION: r6/sdc/extract.py, drop the subject check in
    _extract_by_definition -> red (a Patient entry appears)."""
    bundle = extract_resources(_subject_bound(_intake_like_response()),
                               _intake_like_questionnaire())
    types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert "Patient" not in types
    assert "Synthetic" not in json.dumps(bundle)


def test_submitting_the_same_bound_response_twice_yields_no_patient_either_time():
    q = _intake_like_questionnaire()
    for _ in range(2):
        bundle = extract_resources(_subject_bound(_intake_like_response()), q)
        assert all(e["resource"]["resourceType"] != "Patient" for e in bundle["entry"])


def test_a_subject_less_response_still_posts_a_patient():
    """MUTATION: drop the Patient build entirely -> red."""
    bundle = extract_resources(_intake_like_response(), _intake_like_questionnaire())
    patients = [e for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"]
    assert len(patients) == 1
    assert patients[0]["request"]["method"] == "POST"
    assert patients[0]["resource"]["name"][0]["family"] == "Synthetic"
