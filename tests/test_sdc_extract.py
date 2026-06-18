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
