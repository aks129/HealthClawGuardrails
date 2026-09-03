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


def test_resolves_outside_projection_names_the_paths_the_allowlist_withholds():
    """Withheld -> True; allowlisted -> False. A property of the ALLOWLIST.

    `identifier` and `name.text` are on a Patient and off the allowlist, so
    an item asking for either is refused and says so. The allowlisted paths
    are not, or every intake form would warn about its own demographics.
    """
    for withheld in ("%patient.identifier.value", "%patient.name.text",
                     "%patient.photo.url", "%patient.contact.name.family",
                     "%patient.maritalStatus.text",
                     "%patient.telecom.where(system='sms').value",
                     "%subject.identifier.value", "Patient.identifier.value",
                     "%resources.where(resourceType='Observation').code.text"):
        assert resolves_outside_projection(withheld) is True, withheld
    for allowed in ("%patient.name.family", "%patient.name.given.first()",
                    "%patient.birthDate", "%patient.gender",
                    "%patient.telecom.where(system='phone').value",
                    "%patient.telecom.where(system='email').value",
                    "%patient.address.line.first()",
                    "%patient.address.city.first()",
                    "%patient.address.state.first()",
                    "%patient.address.postalCode.first()",
                    "Patient.name.family"):
        assert resolves_outside_projection(allowed) is False, allowed
    assert resolves_outside_projection("") is False
    assert resolves_outside_projection(None) is False


def test_resolves_outside_projection_never_looks_at_a_patient():
    """THE ONE PROPERTY, and the reason the signature takes no record.

    The answer is a pure function of the expression text. It has to be: the
    version that evaluated the caller's expression against the REAL record
    and reported whether it was non-empty made the issue list a one-bit
    oracle over exactly the data the projection withholds — eleven HTTP
    requests recovered a withheld SSN through it, character by character,
    with the value never appearing in an answer.

    MUTATION: pass the real subject back into the probe -> `walked` becomes
    the SSN and this goes red on the first character.
    """
    victim = dict(_FULL_PATIENT)
    victim["identifier"] = [{"system": "http://hl7.org/fhir/sid/us-ssn",
                             "value": "123-45-6789"}]

    walked = ""
    for _ in range(len("123-45-6789")):
        for ch in "0123456789-":
            probe = ("%patient.identifier.value.where($this.startsWith('"
                     + walked + ch + "'))")
            # The signature admits no record, so nothing about `victim` can
            # steer this. Both a right and a wrong guess answer the same way.
            if resolves_outside_projection(probe):
                walked += ch
                break
        else:
            break
    assert walked == "", (
        f"the withheld identifier leaked through the issue channel: {walked!r}")

    # And the two halves of the guess are indistinguishable, which is what
    # "no oracle" means.
    right = "%patient.identifier.value.where($this='123-45-6789')"
    wrong = "%patient.identifier.value.where($this='999-99-9999')"
    assert resolves_outside_projection(right) == \
        resolves_outside_projection(wrong)


def test_resolves_outside_projection_is_not_a_lever_on_the_tenants_content():
    """The probe is a constant, so a caller cannot buy work with an
    expression that walks the whole record.

    MUTATION: evaluate against the real content_resources -> this exceeds the
    budget by orders of magnitude (measured at 32s for 50 items over 500
    resources during the PR #562 review).
    """
    import time
    expr = "%resources.descendants().descendants()"
    resolves_outside_projection.cache_clear()
    start = time.perf_counter()
    for _ in range(200):
        resolves_outside_projection(expr)
    assert time.perf_counter() - start < 2.0


def test_the_resource_root_form_still_evaluates_against_the_projection():
    """A Questionnaire may write `Patient.name.family` rather than
    `%patient.name.family`. fhirpathpy raises KeyError on that form when the
    resource carries no `resourceType`, and evaluate() swallows the raise as
    "no value" — so the projection keeps the type tag, and this pins it."""
    ctx = build_context(subject=_FULL_PATIENT)
    assert evaluate("Patient.name.family", ctx["patient"], ctx) == "Lovelace"
    assert evaluate("Patient.identifier.value", ctx["patient"], ctx) is None
