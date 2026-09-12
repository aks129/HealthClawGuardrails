"""$extract writes each Patient element in the shape FHIR gives it (#666).

`_set_path` shaped only name.* and birthDate; every other path fell
through to a nested-dict scalar write, so the intake form's own items
produced Patient.telecom as a bare string, Patient.address.line as a dict,
and a second given name appended to the first. Structural validation
cannot see any of that. Measured on main before this change with the
assertions below.

Now every element the engine builds for Patient has a setter with the
element's cardinality — telecom and address are arrays of one entry,
address.line and name.given are arrays inside it, given is replaced per
submission — and a path with no setter is refused through the #681 log
line rather than written as a scalar. The element list and the setters
are one set: DEFINITION_ELEMENTS names exactly what has a shape.

Limit written down: a ContactPoint needs a system with its value (FHIR
cpt-2); a questionnaire that defines only telecom.value produces one
without, which the validator will say. The engine invents nothing.

MUTATION: r6/sdc/extract.py, restore the generic scalar fallback -> red
(telecom is a string again). Drop the address.line list -> red.
"""

import logging

from r6.sdc.extract import DEFINITION_ELEMENTS, extract_resources
from r6.sdc.intake import intake_questionnaire

SD = "http://hl7.org/fhir/StructureDefinition"
DEF_EXTRACT = ("http://hl7.org/fhir/uv/sdc/StructureDefinition/"
               "sdc-questionnaire-definitionExtract")


def _patient_from(pairs):
    q = {"resourceType": "Questionnaire", "status": "active",
         "extension": [{"url": DEF_EXTRACT, "valueCode": "Patient"}],
         "item": [{"linkId": f"i{n}", "type": "string",
                   "definition": f"{SD}/Patient#Patient.{path}"}
                  for n, (path, _v) in enumerate(pairs)]}
    qr = {"resourceType": "QuestionnaireResponse", "status": "completed",
          "item": [{"linkId": f"i{n}", "answer": [{"valueString": v}]}
                   for n, (_p, v) in enumerate(pairs)]}
    entries = extract_resources(qr, q)["entry"]
    return entries[0]["resource"] if entries else None


def test_the_intake_form_s_demographics_have_fhir_shapes():
    patient = _patient_from([
        ("name.family", "Probe"), ("name.given", "Pat"),
        ("birthDate", "1970-01-02"), ("gender", "female"),
        ("telecom.value", "+1 555 0100"),
        ("address.line", "1 Probe St"), ("address.city", "Probeville"),
        ("address.state", "NY"), ("address.postalCode", "10001"),
    ])
    assert patient == {
        "resourceType": "Patient",
        "name": [{"family": "Probe", "given": ["Pat"]}],
        "birthDate": "1970-01-02",
        "gender": "female",
        "telecom": [{"value": "+1 555 0100"}],
        "address": [{"line": ["1 Probe St"], "city": "Probeville",
                     "state": "NY", "postalCode": "10001"}],
    }


def test_a_second_given_name_replaces_the_first_within_one_submission():
    # One answer per item reaches the engine; two items both defining
    # name.given is a questionnaire mistake, and the last one wins rather
    # than the two accumulating into ["Pat", "Pat"] across a repopulate.
    patient = _patient_from([("name.given", "Pat"), ("name.given", "Patricia")])
    assert patient["name"] == [{"given": ["Patricia"]}]


def test_telecom_system_is_shaped_beside_the_value():
    patient = _patient_from([("telecom.system", "phone"),
                             ("telecom.value", "+1 555 0100")])
    assert patient["telecom"] == [{"system": "phone", "value": "+1 555 0100"}]


def test_a_path_with_no_shape_is_refused_not_written_as_a_scalar(caplog):
    caplog.set_level(logging.WARNING, logger="r6.sdc.extract")
    # Every one of these is a real Patient element with no setter, or a
    # sub-path the setters do not know. None may land as a scalar.
    for path in ("telecom", "address", "identifier", "active",
                 "address.period", "name", "maritalStatus"):
        assert _patient_from([(path, "probe-value")]) is None, path
    assert all("probe-value" not in r.getMessage() for r in caplog.records)
    assert sum("#681" in r.getMessage() for r in caplog.records) == 7


def test_the_element_list_is_exactly_what_has_a_shape():
    assert DEFINITION_ELEMENTS["Patient"] == frozenset(
        {"name", "birthDate", "gender", "telecom", "address"})


def test_the_intake_form_defines_only_paths_the_engine_shapes():
    # The form is the one real definition-extract questionnaire; its every
    # Patient path must be one the engine can shape, or the answer is lost.
    q = intake_questionnaire()
    paths = set()

    def walk(items):
        for item in items:
            d = item.get("definition", "")
            if "#Patient." in d:
                paths.add(d.split("#Patient.", 1)[1])
            walk(item.get("item", []))
    walk(q["item"])
    assert paths == {"name.family", "name.given", "birthDate", "gender",
                     "telecom.value", "address.line", "address.city",
                     "address.state", "address.postalCode"}
