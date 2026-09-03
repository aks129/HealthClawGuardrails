from r6.sdc.expressions import (build_context, evaluate,
                                patient_projection)


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


# The three `resolves_outside_projection` tests that stood here are gone with
# the function (CTO ruling on PR #562). They pinned real properties and those
# properties still hold — they moved rather than lapsed:
#
#   - the oracle test (a right guess and a wrong guess must be
#     indistinguishable) is now
#     tests/test_populate_issue_property.py::
#     test_a_right_guess_and_a_wrong_guess_are_indistinguishable, and over
#     HTTP in tests/test_sdc_populate_bounded.py::
#     test_the_withheld_issue_cannot_be_used_to_guess_the_withheld_value;
#   - the signature pin (the classifier must not be able to see a record)
#     is subsumed by there being no classifier: the issue is emitted for
#     every attempted leaf that resolved nothing, which is a fact the
#     response already carries. tests/test_populate_issue_property.py states
#     that as `answer present <=> no issue`, per item, so a reintroduced
#     classifier of ANY shape — record-driven or probe-driven — reddens on
#     the first leaf it silences, which the signature pin could not catch;
#   - the allowlist-membership list is what the projection tests above
#     already assert directly, on the projection itself rather than through
#     a function that guessed at it.


def test_the_resource_root_form_still_evaluates_against_the_projection():
    """A Questionnaire may write `Patient.name.family` rather than
    `%patient.name.family`. fhirpathpy raises KeyError on that form when the
    resource carries no `resourceType`, and evaluate() swallows the raise as
    "no value" — so the projection keeps the type tag, and this pins it."""
    ctx = build_context(subject=_FULL_PATIENT)
    assert evaluate("Patient.name.family", ctx["patient"], ctx) == "Lovelace"
    assert evaluate("Patient.identifier.value", ctx["patient"], ctx) is None
