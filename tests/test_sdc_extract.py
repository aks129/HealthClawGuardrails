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
        "resourceType": "QuestionnaireResponse", "status": "completed",
        "subject": {"reference": "Patient/p-572"},
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
