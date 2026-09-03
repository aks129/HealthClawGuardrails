from r6.sdc.expressions import (build_context, evaluate, patient_projection,
                                resolves_outside_projection)


def test_evaluate_simple_path():
    patient = {"resourceType": "Patient",
               "name": [{"given": ["Ada"], "family": "Lovelace"}]}
    assert evaluate("Patient.name.given.first()", patient) == "Ada"


def test_evaluate_with_launch_context_variable():
    patient = {"resourceType": "Patient", "birthDate": "1990-01-01"}
    ctx = build_context(subject=patient)
    assert evaluate("%patient.birthDate", patient, ctx) == "1990-01-01"


def test_evaluate_returns_none_on_no_match():
    patient = {"resourceType": "Patient"}
    assert evaluate("Patient.name.given.first()", patient) is None


def test_evaluate_returns_none_on_bad_expression():
    assert evaluate("this is not fhirpath (((", {}) is None


# ---------------------------------------------------------------------------
# The %patient projection (council ruling D10)
# ---------------------------------------------------------------------------

_FULL_PATIENT = {
    "resourceType": "Patient",
    "id": "p1",
    "name": [{"given": ["Ada"], "family": "Lovelace", "text": "Ada Lovelace",
              "prefix": ["Countess"]}],
    "birthDate": "1815-12-10",
    "gender": "female",
    "identifier": [{"system": "http://hospital.example/mrn", "value": "MRN-1"}],
    "photo": [{"contentType": "image/jpeg",
               "url": "https://example.org/a.jpg"}],
    "telecom": [{"system": "phone", "value": "555-0100", "use": "home"},
                {"system": "email", "value": "ada@example.org"},
                {"system": "url", "value": "https://example.org/ada"}],
    "address": [{"line": ["1 Analytical Way"], "city": "London", "state": "MA",
                 "postalCode": "01001", "country": "GB",
                 "text": "1 Analytical Way, London"}],
    "contact": [{"name": {"family": "Byron"},
                 "telecom": [{"system": "phone", "value": "555-0199"}]}],
    "extension": [{"url": "http://example.org/x", "valueString": "secret"}],
}


def test_the_projection_carries_only_the_allowlisted_elements():
    """An allowlist asserted as an exact set, not a spot check.

    Naming the keys that must be ABSENT one at a time would go green the day
    a new PHI-bearing element arrives in the source; asserting the whole
    shape means anything not on the list fails here.
    """
    assert patient_projection(_FULL_PATIENT) == {
        "resourceType": "Patient",
        "name": [{"given": ["Ada"], "family": "Lovelace"}],
        "birthDate": "1815-12-10",
        "gender": "female",
        "telecom": [{"system": "phone", "value": "555-0100"},
                    {"system": "email", "value": "ada@example.org"}],
        "address": [{"line": ["1 Analytical Way"], "city": "London",
                     "state": "MA", "postalCode": "01001"}],
    }


def test_the_projection_is_a_new_object_not_a_view_of_the_record():
    """Mutating the projection must not reach the stored resource, and the
    lists inside it must not be the record's lists."""
    projection = patient_projection(_FULL_PATIENT)
    projection["name"][0]["family"] = "Changed"
    projection["telecom"].append({"system": "phone", "value": "999"})
    assert _FULL_PATIENT["name"][0]["family"] == "Lovelace"
    assert len(_FULL_PATIENT["telecom"]) == 3


def test_the_environment_holds_the_projection_and_nothing_else():
    """%patient and %subject only. There is deliberately no %resources — a
    questionnaire author is not handed the tenant's clinical bundle."""
    ctx = build_context(subject=_FULL_PATIENT)
    assert set(ctx) == {"patient", "subject"}
    assert ctx["patient"] == ctx["subject"] == patient_projection(_FULL_PATIENT)


def test_build_context_with_no_subject_is_empty():
    assert build_context() == {}
    assert build_context(subject=None) == {}
    assert patient_projection("not a resource") is None


def test_resolves_outside_projection_tells_withheld_from_absent():
    """THE ONE PROPERTY: True only when something WOULD have resolved.

    `identifier` is on the record and withheld by the projection -> True, and
    the caller (which only asks after its own bounded evaluation came back
    empty) turns that into an issue. `maritalStatus` is on neither the record
    nor the allowlist -> False: nothing was withheld, the record simply does
    not have it, and an issue there would be noise on every sparse record.
    """
    assert resolves_outside_projection("%patient.identifier.value",
                                       _FULL_PATIENT) is True
    assert resolves_outside_projection("%patient.name.text",
                                       _FULL_PATIENT) is True
    assert resolves_outside_projection("%patient.maritalStatus.text",
                                       _FULL_PATIENT) is False


def test_resolves_outside_projection_returns_a_bool_and_never_the_value():
    """The unbounded evaluation exists to answer a yes/no question. If it
    could hand its value back, the projection would have a bypass with a
    helpful name."""
    result = resolves_outside_projection("%patient.identifier.value",
                                         _FULL_PATIENT)
    assert result is True
    assert not isinstance(result, str)


def test_resolves_outside_projection_sees_the_content_resources():
    """%resources is absent from the bounded environment, so an expression
    reaching for it is REPORTED rather than silently empty."""
    obs = {"resourceType": "Observation",
           "code": {"text": "Cholesterol (total)"}}
    expr = "%resources.where(resourceType='Observation').code.text"
    assert resolves_outside_projection(expr, _FULL_PATIENT, [obs]) is True
    assert resolves_outside_projection(expr, _FULL_PATIENT, []) is False


def test_the_resource_root_form_still_evaluates_against_the_projection():
    """A Questionnaire may write `Patient.name.family` rather than
    `%patient.name.family`. fhirpathpy raises KeyError on that form when the
    resource carries no `resourceType`, and evaluate() swallows the raise as
    "no value" — so the projection keeps the type tag, and this pins it."""
    ctx = build_context(subject=_FULL_PATIENT)
    assert evaluate("Patient.name.family", ctx["patient"], ctx) == "Lovelace"
    assert evaluate("Patient.identifier.value", ctx["patient"], ctx) is None
