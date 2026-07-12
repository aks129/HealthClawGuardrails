"""The canonical `healthclaw-intake` Questionnaire.

This is the standard new-patient intake the form-fill rail populates from a
patient's FHIR data via SDC `$populate` and writes back via `$extract`
(r6/sdc/populate.py, r6/sdc/extract.py).

Sections:
  - demographics: leaf items with both `definition` (Patient element path,
    for $extract) and `initialExpression` (FHIRPath, for $populate) — these
    populate today because populate.py already resolves Patient-based
    initialExpressions.
  - medications / allergies / conditions: repeating `<section>.item` groups
    structured with `definition` pointing at the relevant resource type
    (MedicationRequest / AllergyIntolerance / Condition). They intentionally
    carry NO initialExpression yet — populate.py only matches Observations
    by item.code and Patient-rooted FHIRPath today. Wiring these groups to
    fill one repeat per matching list resource is Task 2.

NKA (no-known-allergies) invariant — READ THIS BEFORE TOUCHING
`allergies.no-known-allergies`:
    "No known allergies" must be a distinct affirmative attestation the
    patient (or someone on their behalf) explicitly makes. It must NEVER
    carry an `initial` value and must NEVER be filled by an
    initialExpression, because silence about allergies is not consent —
    a default here would let the form-fill rail present "no known
    allergies" for a patient who was simply never asked, which is a
    patient-safety hazard. Task 2's list-resource population must not
    touch this item; it stays operator/patient-entered only.
    Enforced by tests/test_intake_questionnaire.py::
    test_no_known_allergies_invariant_never_defaulted.
"""

import copy

INITIAL_EXPRESSION_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-initialExpression"
)

# NOTE: the root definitionExtract flag below only ever names ONE target
# resource type (see r6/sdc/extract.py:_extract_by_definition) — a v1
# limitation of the existing engine, not something introduced here. It's set
# to Patient because demographics is the only section that both populates
# and extracts correctly today. The `definition` values on the medications /
# allergies / conditions items point at their real target resource types
# (MedicationRequest / AllergyIntolerance / Condition) for Task 2 to wire up;
# until then those items simply won't have answers for extract to touch.
DEFINITION_EXTRACT_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-definitionExtract"
)

PATIENT_DEF = "http://hl7.org/fhir/StructureDefinition/Patient#Patient"
MED_REQUEST_DEF = (
    "http://hl7.org/fhir/StructureDefinition/MedicationRequest#MedicationRequest"
)
ALLERGY_DEF = (
    "http://hl7.org/fhir/StructureDefinition/AllergyIntolerance#AllergyIntolerance"
)
CONDITION_DEF = "http://hl7.org/fhir/StructureDefinition/Condition#Condition"


def _initial_expr(expression):
    return [{
        "url": INITIAL_EXPRESSION_URL,
        "valueExpression": {"language": "text/fhirpath", "expression": expression},
    }]


INTAKE_QUESTIONNAIRE = {
    "resourceType": "Questionnaire",
    "id": "healthclaw-intake",
    "url": "http://healthclaw.io/fhir/Questionnaire/healthclaw-intake",
    "version": "1.0.0",
    "status": "active",
    "title": "HealthClaw Standard Intake",
    "extension": [{"url": DEFINITION_EXTRACT_URL, "valueCode": "Patient"}],
    "item": [
        {
            "linkId": "demographics",
            "type": "group",
            "text": "Demographics",
            "item": [
                {
                    "linkId": "demographics.given-name",
                    "type": "string",
                    "text": "First name",
                    "definition": f"{PATIENT_DEF}.name.given",
                    "extension": _initial_expr("%patient.name.given.first()"),
                },
                {
                    "linkId": "demographics.family-name",
                    "type": "string",
                    "text": "Last name",
                    "definition": f"{PATIENT_DEF}.name.family",
                    "extension": _initial_expr("%patient.name.family"),
                },
                {
                    "linkId": "demographics.birth-date",
                    "type": "date",
                    "text": "Date of birth",
                    "definition": f"{PATIENT_DEF}.birthDate",
                    "extension": _initial_expr("%patient.birthDate"),
                },
                {
                    "linkId": "demographics.gender",
                    "type": "choice",
                    "text": "Administrative gender",
                    "definition": f"{PATIENT_DEF}.gender",
                    "extension": _initial_expr("%patient.gender"),
                    "answerOption": [
                        {"valueCoding": {
                            "system": "http://hl7.org/fhir/administrative-gender",
                            "code": "male", "display": "Male"}},
                        {"valueCoding": {
                            "system": "http://hl7.org/fhir/administrative-gender",
                            "code": "female", "display": "Female"}},
                        {"valueCoding": {
                            "system": "http://hl7.org/fhir/administrative-gender",
                            "code": "other", "display": "Other"}},
                        {"valueCoding": {
                            "system": "http://hl7.org/fhir/administrative-gender",
                            "code": "unknown", "display": "Unknown"}},
                    ],
                },
                {
                    "linkId": "demographics.phone",
                    "type": "string",
                    "text": "Phone number",
                    "definition": f"{PATIENT_DEF}.telecom",
                    "extension": _initial_expr(
                        "%patient.telecom.where(system='phone').value.first()"),
                },
                {
                    "linkId": "demographics.address-line",
                    "type": "string",
                    "text": "Street address",
                    "definition": f"{PATIENT_DEF}.address.line",
                    "extension": _initial_expr(
                        "%patient.address.line.first()"),
                },
                {
                    "linkId": "demographics.address-city",
                    "type": "string",
                    "text": "City",
                    "definition": f"{PATIENT_DEF}.address.city",
                    "extension": _initial_expr("%patient.address.city.first()"),
                },
                {
                    "linkId": "demographics.address-state",
                    "type": "string",
                    "text": "State",
                    "definition": f"{PATIENT_DEF}.address.state",
                    "extension": _initial_expr(
                        "%patient.address.state.first()"),
                },
                {
                    "linkId": "demographics.address-postal-code",
                    "type": "string",
                    "text": "Postal code",
                    "definition": f"{PATIENT_DEF}.address.postalCode",
                    "extension": _initial_expr(
                        "%patient.address.postalCode.first()"),
                },
            ],
        },
        {
            "linkId": "medications",
            "type": "group",
            "text": "Current medications",
            "item": [
                {
                    "linkId": "medications.no-current-medications",
                    "type": "boolean",
                    "text": "Patient confirms no current medications",
                },
                {
                    "linkId": "medications.item",
                    "type": "group",
                    "text": "Medication",
                    "repeats": True,
                    "item": [
                        {
                            "linkId": "medications.item.name",
                            "type": "string",
                            "text": "Medication name",
                            "definition":
                                f"{MED_REQUEST_DEF}.medicationCodeableConcept.text",
                        },
                        {
                            "linkId": "medications.item.dose",
                            "type": "string",
                            "text": "Dose",
                            "definition":
                                f"{MED_REQUEST_DEF}.dosageInstruction.text",
                        },
                    ],
                },
            ],
        },
        {
            "linkId": "allergies",
            "type": "group",
            "text": "Allergies",
            "item": [
                {
                    # CRITICAL INVARIANT — see module docstring. Never add
                    # `initial` or an initialExpression extension here: NKA
                    # must stay a distinct affirmative attestation, never a
                    # default and never inferred from absent allergy data.
                    "linkId": "allergies.no-known-allergies",
                    "type": "boolean",
                    "text": "No known allergies (patient confirmed)",
                    "required": True,
                },
                {
                    "linkId": "allergies.item",
                    "type": "group",
                    "text": "Allergy",
                    "repeats": True,
                    "item": [
                        {
                            "linkId": "allergies.item.allergen",
                            "type": "string",
                            "text": "Allergen",
                            "definition": f"{ALLERGY_DEF}.code.text",
                        },
                        {
                            "linkId": "allergies.item.reaction",
                            "type": "string",
                            "text": "Reaction",
                            "definition":
                                f"{ALLERGY_DEF}.reaction.manifestation.text",
                        },
                    ],
                },
            ],
        },
        {
            "linkId": "conditions",
            "type": "group",
            "text": "Problems / conditions",
            "item": [
                {
                    "linkId": "conditions.item",
                    "type": "group",
                    "text": "Condition",
                    "repeats": True,
                    "item": [
                        {
                            "linkId": "conditions.item.name",
                            "type": "string",
                            "text": "Condition name",
                            "definition": f"{CONDITION_DEF}.code.text",
                        },
                    ],
                },
            ],
        },
    ],
}


def intake_questionnaire() -> dict:
    """Return a fresh deep copy of the canonical intake Questionnaire.

    Callers (seed.py, tests) get their own copy so nothing accidentally
    mutates the shared module-level template.
    """
    return copy.deepcopy(INTAKE_QUESTIONNAIRE)
