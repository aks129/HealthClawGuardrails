from r6.sdc.populate import NOT_POPULATED, populate_questionnaire

INITIAL_EXPR_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-initialExpression"
)


def _expr_item(link_id, expression):
    return {
        "linkId": link_id,
        "type": "string",
        "extension": [{
            "url": INITIAL_EXPR_URL,
            "valueExpression": {"language": "text/fhirpath",
                                "expression": expression},
        }],
    }


def test_populate_initial_expression():
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [_expr_item("first-name",
                             "%patient.name.given.first()")]}
    patient = {"resourceType": "Patient", "id": "p1",
               "name": [{"given": ["Ada"]}]}

    qr, issues = populate_questionnaire(q, patient, [patient])

    assert qr["resourceType"] == "QuestionnaireResponse"
    assert qr["status"] == "in-progress"
    assert qr["subject"] == {"reference": "Patient/p1"}
    answer_item = qr["item"][0]
    assert answer_item["linkId"] == "first-name"
    assert answer_item["answer"][0]["valueString"] == "Ada"
    assert issues == []


def test_populate_observation_based_by_code():
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{
             "linkId": "weight",
             "type": "quantity",
             "code": [{"system": "http://loinc.org", "code": "29463-7"}],
         }]}
    patient = {"resourceType": "Patient", "id": "p1"}
    obs = {"resourceType": "Observation", "status": "final",
           "code": {"coding": [{"system": "http://loinc.org",
                                "code": "29463-7"}]},
           "subject": {"reference": "Patient/p1"},
           "valueQuantity": {"value": 70, "unit": "kg"}}

    qr, issues = populate_questionnaire(q, patient, [patient, obs])

    answer = qr["item"][0]["answer"][0]
    assert answer["valueQuantity"]["value"] == 70
    assert issues == []


def test_populate_records_issue_for_unresolved_item():
    """An item.code that matched no Observation is an ATTEMPTED population.

    It reports for the same reason an initialExpression does: the caller
    asked the server to resolve something and it did not. The issue is
    `information`, not an error — "absence of data is not an error" still
    holds, and the text says only that no value resolved.

    The intake Questionnaire carries no item.code items, so this path is
    invisible on today's form; another Questionnaire will have them, which
    is why it is pinned here rather than left to the integration tests.
    """
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{"linkId": "missing", "type": "string",
                   "code": [{"system": "http://loinc.org", "code": "0000-0"}]}]}
    patient = {"resourceType": "Patient", "id": "p1"}

    qr, issues = populate_questionnaire(q, patient, [patient])

    # No answer produced, and no spurious answer array on the item.
    assert "answer" not in qr["item"][0]
    # The issue carries the linkId and nothing else: the reason is the same
    # constant for every leaf and is said once per response by
    # r6/sdc/routes.py:_issues_outcome.
    assert issues == [{"linkId": "missing"}]
    assert "no value resolved" in NOT_POPULATED


def test_populate_nested_group_items():
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{"linkId": "demographics", "type": "group",
                   "item": [_expr_item("dob", "%patient.birthDate")]}]}
    patient = {"resourceType": "Patient", "id": "p1",
               "birthDate": "1815-12-10"}

    qr, _ = populate_questionnaire(q, patient, [patient])

    group = qr["item"][0]
    assert group["linkId"] == "demographics"
    assert group["item"][0]["answer"][0]["valueString"] == "1815-12-10"
