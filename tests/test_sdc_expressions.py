import logging

from r6.sdc.expressions import (build_context, evaluate,
                                patient_projection)
from r6.sdc.populate import INITIAL_EXPRESSION_URL, populate_questionnaire


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


# ---------------------------------------------------------------------------
# What an evaluation failure is allowed to log
# ---------------------------------------------------------------------------

#: A Questionnaire an attacker can POST. `notAFunction()` makes fhirpathpy
#: raise, and everything after the newline is a log line forged to look like
#: the access kernel granting a step-up.
FORGED = ("notAFunction()\n"
          "2026-09-03 18:00:00 - r6.access - WARNING - step-up token "
          "accepted for tenant admin")


def test_a_failing_expression_is_not_echoed_into_the_log(caplog):
    """The expression text is caller-supplied, so it does not get logged.

    A Questionnaire is request body: its expressions carry whatever the
    caller wrote, newlines and a forged WARNING line included, and on this
    path they can also carry literals the caller chose. Logging them back
    hands an attacker the log file's contents (CTO ruling on #562, "not
    blocking, please file"). What is logged instead is the linkId — which is
    questionnaire structure, not patient data — and the exception's CLASS
    name. Not `str(exc)`: fhirpathpy puts the offending token in the message
    ("Not implemented: notAFunction"), which is the caller's text arriving
    by the back door.

    Asserted on the LogRecord rather than on `caplog.text`, because the two
    disagree in exactly the way that matters. `%r` escapes the newline when
    the record is formatted, so a text assertion goes green on a forged log
    line while the raw expression is still sitting in `record.args`, where
    any handler or formatter that reads it — a structured one, a shipper —
    gets the unescaped string. (This app's own JSONFormatter, main.py:36,
    happens to use `getMessage()` and would not; the record is still where
    the untrusted text is, and the assertion should not depend on which
    formatter is installed.)

    Driven through `populate_questionnaire` rather than `evaluate` directly,
    so the linkId plumbing is part of what is pinned: an item whose
    expression fails must be identifiable from the log alone.
    """
    questionnaire = {
        "resourceType": "Questionnaire", "status": "active",
        "item": [{
            "linkId": "demographics.phone", "type": "string",
            "extension": [{
                "url": INITIAL_EXPRESSION_URL,
                "valueExpression": {"language": "text/fhirpath",
                                    "expression": FORGED}}],
        }],
    }
    with caplog.at_level(logging.WARNING, logger="r6.sdc.expressions"):
        populate_questionnaire(questionnaire, {"resourceType": "Patient"}, [])

    records = [r for r in caplog.records if r.name == "r6.sdc.expressions"]
    assert len(records) == 1, "expected exactly one failure log record"
    record = records[0]

    parts = [str(record.msg), record.getMessage()]
    parts += [str(arg) for arg in (record.args or ())]
    for part in parts:
        assert "notAFunction" not in part, (
            "the caller's expression text reached the log record")
        assert "step-up token accepted" not in part, (
            "a caller forged a log line through an expression")
        assert "\n" not in part

    link_id, exc_type = record.args
    assert link_id == "demographics.phone"
    # A class name, not a message: `str(exc)` here is "Not implemented:
    # notAFunction", which has spaces and a colon and would fail this.
    assert exc_type.isidentifier(), (
        f"{exc_type!r} is not a bare exception class name")


def test_the_resource_root_form_still_evaluates_against_the_projection():
    """A Questionnaire may write `Patient.name.family` rather than
    `%patient.name.family`. fhirpathpy raises KeyError on that form when the
    resource carries no `resourceType`, and evaluate() swallows the raise as
    "no value" — so the projection keeps the type tag, and this pins it."""
    ctx = build_context(subject=_FULL_PATIENT)
    assert evaluate("Patient.name.family", ctx["patient"], ctx) == "Lovelace"
    assert evaluate("Patient.identifier.value", ctx["patient"], ctx) is None


def test_a_failing_expression_in_a_list_row_logs_once_not_once_per_record(
        caplog):
    """LOG VOLUME DOES NOT SCALE WITH THE CALLER'S RECORD COUNT.

    A leaf inside a `repeats: true` list group is evaluated once per row —
    that is what makes an expression leaf answer there at all, and it is
    deliberate. The failure LOG is a different matter: the line is identical
    for every row (a linkId and an exception class name), so 100 records
    meant 100 identical warnings, and the records arrive in the caller's own
    inline `content` Bundle. Cost that scales with attacker-supplied input
    is worth removing while it is one small change.

    Deduped at the log site (r6/sdc/expressions.py's `warned`), NOT by
    evaluating once per group: evaluating once per group would rebuild a
    second resolution path beside the one _populate_list_children was just
    collapsed into, which is the divergence this branch exists to remove.
    Resolution is untouched — every row still evaluates, and every row still
    gets its own issue.

    MUTATION: drop the `warned` guard -> the 10-record case logs 10 lines.
    """
    questionnaire = {
        "resourceType": "Questionnaire", "status": "active",
        "item": [{
            "linkId": "allergies.item", "type": "group", "repeats": True,
            "item": [
                {"linkId": "a.allergen", "type": "string",
                 "definition": ("http://hl7.org/fhir/StructureDefinition/"
                                "AllergyIntolerance#AllergyIntolerance."
                                "code.text")},
                {"linkId": "a.bad", "type": "string", "extension": [{
                    "url": INITIAL_EXPRESSION_URL,
                    "valueExpression": {"language": "text/fhirpath",
                                        "expression": "notAFunction()"}}]},
            ],
        }],
    }
    patient = {"resourceType": "Patient", "id": "p1"}

    for record_count in (1, 10, 100):
        allergies = [
            {"resourceType": "AllergyIntolerance", "id": f"a{i}",
             "patient": {"reference": "Patient/p1"},
             "code": {"text": f"Allergen {i}"}}
            for i in range(record_count)
        ]
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="r6.sdc.expressions"):
            qr, issues = populate_questionnaire(questionnaire, patient,
                                                allergies)

        records = [r for r in caplog.records if r.name == "r6.sdc.expressions"]
        assert len(records) == 1, (
            f"{record_count} records produced {len(records)} log lines; the "
            f"caller supplies the records, so this must not scale with them")

        # Resolution is unchanged: still one row per record, and still one
        # issue per unresolved leaf occurrence — the dedupe is the log only.
        rows = [x for x in qr["item"] if x["linkId"] == "allergies.item"]
        assert len(rows) == record_count
        assert sum(1 for i in issues if i["linkId"] == "a.bad") == record_count
