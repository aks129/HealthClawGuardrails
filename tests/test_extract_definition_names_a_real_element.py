"""$extract's definition path must name an element the target type has
(#681, the class around #664).

#664 stops an answer whose definition names another resource type from
landing on the target. Both halves of a definition are authored by
whoever writes the questionnaire, so declaring them consistently costs
nothing, and the original harm returns: `Patient#Patient.code.text`
passes the type check and writes an allergen into an element Patient does
not have; `Patient.notAnElement.at.all` writes a nested dict nobody asked
for; `Patient.deceasedBoolean` writes a string where FHIR wants a boolean.
Measured on main before this change: all three landed in the preview
bundle.

Now the segment after the type must be an element the engine knows the
target has. The element lists are per type and deliberately short: only
the types this engine is written to build have one (Patient, for the
intake form), and a type without a list extracts nothing — a bundle with
no entry, and a log line naming the item and the type, never the answer.
Adding a type is a decision made in DEFINITION_ELEMENTS, with a test.

MUTATION: r6/sdc/extract.py, drop the element check -> red (three rows
land). Add "code" to Patient's list -> red (the allergen lands).
"""

import logging

from r6.sdc.extract import DEFINITION_ELEMENTS, extract_resources

SD = "http://hl7.org/fhir/StructureDefinition"
DEF_EXTRACT = ("http://hl7.org/fhir/uv/sdc/StructureDefinition/"
               "sdc-questionnaire-definitionExtract")


def _questionnaire(target, paths):
    return {"resourceType": "Questionnaire", "status": "active",
            "extension": [{"url": DEF_EXTRACT, "valueCode": target}],
            "item": [{"linkId": f"i{n}", "type": "string",
                      "definition": f"{SD}/{target}#{path}"}
                     for n, path in enumerate(paths)]}


def _response(values):
    return {"resourceType": "QuestionnaireResponse", "status": "completed",
            "item": [{"linkId": f"i{n}", "answer": [{"valueString": v}]}
                     for n, v in enumerate(values)]}


def _entries(target, paths, values):
    return extract_resources(_response(values),
                             _questionnaire(target, paths))["entry"]


def test_an_element_patient_does_not_have_is_not_written(caplog):
    caplog.set_level(logging.WARNING, logger="r6.sdc.extract")
    entries = _entries("Patient", [
        "Patient.code.text",            # the #572 allergen, via #681
        "Patient.notAnElement.at.all",
        "Patient.deceasedBoolean",      # a string where FHIR wants a boolean
        "Patient.name.family",          # a real element, still extracted
    ], ["peanut-probe", "peanut-probe", "peanut-probe", "Probefamily"])
    assert len(entries) == 1
    patient = entries[0]["resource"]
    assert patient == {"resourceType": "Patient",
                       "name": [{"family": "Probefamily"}]}
    dropped = [r.getMessage() for r in caplog.records if "#681" in r.getMessage()]
    assert len(dropped) == 3
    assert all("peanut-probe" not in m for m in dropped)
    assert any("'i0'" in m and "Patient.code" in m for m in dropped)


def test_a_bare_type_path_names_no_element():
    assert _entries("Patient", ["Patient"], ["x"]) == []


def test_a_type_this_engine_does_not_build_extracts_nothing(caplog):
    caplog.set_level(logging.WARNING, logger="r6.sdc.extract")
    assert _entries("Consent", ["Consent.status"], ["active"]) == []
    assert any("#681" in r.getMessage() and "Consent" in r.getMessage()
               for r in caplog.records)


def test_the_element_lists_are_for_the_types_this_engine_builds():
    # Adding a type is a decision, made here, on purpose, with a test.
    assert set(DEFINITION_ELEMENTS) == {"Patient"}
    for path in ("name", "telecom", "gender", "birthDate", "address"):
        assert path in DEFINITION_ELEMENTS["Patient"]   # the intake form
    assert "code" not in DEFINITION_ELEMENTS["Patient"]
