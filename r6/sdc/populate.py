"""SDC $populate engine — Questionnaire + subject + content -> QuestionnaireResponse.

Pure function (no DB, no Flask). Supports three SDC population mechanisms:
  - Expression-based: items carrying an initialExpression (FHIRPath).
  - Observation-based: items with an item.code (LOINC) matched against
    Observations in the supplied content.
  - List-resource-based: a `repeats: true` group whose leaves carry a
    `definition` naming a supported resource type (MedicationRequest /
    AllergyIntolerance / Condition) emits one repeat of the group per
    matching resource for the subject (see _populate_list_group).

Out of scope (v1): StructureMap-based and CQL populate.

SAFETY INVARIANT — read before touching list-resource population:
`allergies.no-known-allergies` (and `medications.no-current-medications`)
must NEVER be auto-answered here. They carry no `code` and no
initialExpression (see r6/sdc/intake.py), so the ordinary leaf-resolution
path already leaves them unanswered — and list-group population (this
module) only ever touches the *repeating* `<section>.item` groups, never
those sibling booleans. Zero matching resources means the repeating group
simply contributes zero repeats; it never flips a boolean to make up for
the absence. Enforced by tests/test_populate_lists.py::
test_zero_allergies_never_infers_no_known_allergies (the load-bearing one)
and test_zero_medications_never_infers_no_current_medications.
"""

from r6.sdc.expressions import build_context, evaluate

INITIAL_EXPRESSION_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-initialExpression"
)

# FHIR item.type -> QuestionnaireResponse.answer value[x] key for scalars.
_ANSWER_KEY_BY_TYPE = {
    "boolean": "valueBoolean",
    "decimal": "valueDecimal",
    "integer": "valueInteger",
    "date": "valueDate",
    "dateTime": "valueDateTime",
    "time": "valueTime",
    "string": "valueString",
    "text": "valueString",
    "url": "valueUri",
    "quantity": "valueQuantity",
}


def _codeable_concept_text(concept):
    """Best-effort human text for a CodeableConcept: .text, else first coding.display."""
    if not concept:
        return None
    text = concept.get("text")
    if text:
        return text
    for coding in concept.get("coding", []):
        display = coding.get("display")
        if display:
            return display
    return None


def _medication_name(resource):
    return _codeable_concept_text(resource.get("medicationCodeableConcept"))


def _medication_dose(resource):
    instructions = resource.get("dosageInstruction") or []
    if not instructions:
        return None
    return instructions[0].get("text")


def _allergy_allergen(resource):
    return _codeable_concept_text(resource.get("code"))


def _allergy_reaction(resource):
    for reaction in resource.get("reaction") or []:
        for manifestation in reaction.get("manifestation") or []:
            text = _codeable_concept_text(manifestation)
            if text:
                return text
    return None


def _condition_name(resource):
    return _codeable_concept_text(resource.get("code"))


_ACTIVE_MEDICATION_STATUSES = {"active", "on-hold"}
_EXCLUDED_VERIFICATION_STATUSES = {"entered-in-error", "refuted"}
_ACTIVE_CONDITION_CLINICAL_STATUSES = {"active", "recurrence", "relapse"}
_CONFIRMED_CONDITION_VERIFICATION_STATUSES = {"confirmed", "provisional", "differential"}


def _status_codes(resource, field):
    return {c.get("code") for c in (resource.get(field) or {}).get("coding", [])}


def _medication_request_included(resource):
    return resource.get("status") in _ACTIVE_MEDICATION_STATUSES


def _allergy_intolerance_included(resource):
    verification = _status_codes(resource, "verificationStatus")
    return not (verification & _EXCLUDED_VERIFICATION_STATUSES)


def _condition_included(resource):
    clinical = _status_codes(resource, "clinicalStatus")
    verification = _status_codes(resource, "verificationStatus")
    if clinical and not (clinical & _ACTIVE_CONDITION_CLINICAL_STATUSES):
        return False
    if verification and not (verification & _CONFIRMED_CONDITION_VERIFICATION_STATUSES):
        return False
    return True


# Per-resource-type: which element (definition path) resolves to which value,
# which field on the resource holds the subject reference (R4 is
# inconsistent here — AllergyIntolerance uses `patient`, not `subject`), and
# a status/verification filter deciding whether a resource is "current"
# enough to surface on the intake form. These map the *concrete* element
# paths used by r6/sdc/intake.py — this is intentionally not a general
# FHIRPath engine (see module docstring).
_LIST_RESOURCE_CONFIG = {
    "MedicationRequest": {
        "subject_field": "subject",
        "included": _medication_request_included,
        "resolvers": {
            "medicationCodeableConcept.text": _medication_name,
            "dosageInstruction.text": _medication_dose,
        },
    },
    "AllergyIntolerance": {
        "subject_field": "patient",
        "included": _allergy_intolerance_included,
        "resolvers": {
            "code.text": _allergy_allergen,
            "reaction.manifestation.text": _allergy_reaction,
        },
    },
    "Condition": {
        "subject_field": "subject",
        "included": _condition_included,
        "resolvers": {
            "code.text": _condition_name,
        },
    },
}


def populate_questionnaire(questionnaire, subject, content_resources):
    """Return (questionnaire_response, issues).

    questionnaire: Questionnaire dict.
    subject: Patient dict (or None).
    content_resources: list of resource dicts available for population
        (should include the subject and any Observations).
    issues: list of {'linkId', 'detail'} for items that errored (not for
        items that simply had no data).
    """
    issues = []
    context = build_context(subject=subject, resources=content_resources)
    observations = [r for r in (content_resources or [])
                    if r.get("resourceType") == "Observation"]

    answer_items = []
    for item in questionnaire.get("item", []):
        answer_items.extend(_populate_item(
            item, subject, context, observations, issues, content_resources))

    qr = {
        "resourceType": "QuestionnaireResponse",
        "status": "in-progress",
        "questionnaire": _questionnaire_canonical(questionnaire),
        "item": answer_items,
    }
    subject_ref = _reference(subject)
    if subject_ref:
        qr["subject"] = subject_ref
    return qr, issues


def _populate_item(item, subject, context, observations, issues, content_resources):
    """Populate one questionnaire item. Always returns a list of zero or more
    QuestionnaireResponse items (zero or one for ordinary items; zero or many
    for a repeating list-resource group — one per matching resource).
    """
    link_id = item.get("linkId")
    item_type = item.get("type")

    if item_type == "group":
        list_resource_type = _list_group_resource_type(item)
        if item.get("repeats") and list_resource_type:
            return _populate_list_group(item, list_resource_type, subject,
                                        content_resources)
        # Ordinary group: recurse, keep the group only if it produced
        # child answers.
        children = []
        for child in item.get("item", []):
            children.extend(_populate_item(
                child, subject, context, observations, issues,
                content_resources))
        if not children:
            return []
        return [{"linkId": link_id, "item": children}]

    answer_value, value_key = _resolve_answer(
        item, item_type, context, observations, issues, link_id)
    # Leaf items are always emitted so the response mirrors the questionnaire's
    # structure; the answer array is attached only when a value resolved.
    answer_item = {"linkId": link_id}
    if answer_value is not None:
        answer_item["answer"] = [{value_key: answer_value}]
    return [answer_item]


def _list_group_resource_type(item):
    """Return the resource type this group's leaves are `definition`-linked
    to, if it's a recognized list-resource group (see _LIST_RESOURCE_CONFIG);
    None otherwise.
    """
    for child in item.get("item", []):
        resource_type, _element_path = _parse_definition(child.get("definition"))
        if resource_type in _LIST_RESOURCE_CONFIG:
            return resource_type
    return None


def _parse_definition(definition):
    """Split a `<StructureDefinition url>#<Type>.<element.path>` definition
    into (resource_type, element_path). Returns (None, None) if not parseable.
    """
    if not definition or "#" not in definition:
        return None, None
    path = definition.split("#", 1)[1]
    parts = path.split(".", 1)
    if len(parts) != 2:
        return None, None
    return parts[0], parts[1]


def _populate_list_group(item, resource_type, subject, content_resources):
    """Emit one repeat of `item` per matching, currently-relevant resource of
    `resource_type` for `subject`. Returns [] when there are none — an empty
    repeating group, never a default answer for a sibling item (see module
    docstring's safety invariant).
    """
    config = _LIST_RESOURCE_CONFIG[resource_type]
    subject_ref = _reference(subject)
    resources = [
        r for r in (content_resources or [])
        if r.get("resourceType") == resource_type
        and _references_subject(r, config["subject_field"], subject_ref)
        and config["included"](r)
    ]

    repeats = []
    for resource in resources:
        children = []
        for child in item.get("item", []):
            child_resource_type, element_path = _parse_definition(
                child.get("definition"))
            resolver = None
            if child_resource_type == resource_type:
                resolver = config["resolvers"].get(element_path)
            value = resolver(resource) if resolver else None
            child_item = {"linkId": child.get("linkId")}
            if value is not None:
                value_key = _ANSWER_KEY_BY_TYPE.get(child.get("type"), "valueString")
                child_item["answer"] = [{value_key: value}]
            children.append(child_item)
        repeats.append({"linkId": item.get("linkId"), "item": children})
    return repeats


def _references_subject(resource, subject_field, subject_ref):
    """True if `resource[subject_field].reference` matches subject_ref, or if
    there's no subject_ref to check against (permissive, matching the
    existing Observation-matching behavior which doesn't filter by subject
    either — content_resources is the caller's scoping responsibility).
    """
    if not subject_ref:
        return True
    ref = (resource.get(subject_field) or {}).get("reference")
    return ref == subject_ref.get("reference")


def _resolve_answer(item, item_type, context, observations, issues, link_id):
    value_key = _ANSWER_KEY_BY_TYPE.get(item_type, "valueString")

    expr = _initial_expression(item)
    if expr:
        value = evaluate(expr, context.get("patient"), context)
        if value is not None:
            return _coerce(value, item_type), value_key
        return None, value_key

    codes = item.get("code") or []
    if codes:
        value = _observation_answer(codes, observations)
        if value is not None:
            return value, value_key
    return None, value_key


def _observation_answer(item_codes, observations):
    """Return the most recent Observation value matching any item code."""
    wanted = {(c.get("system"), c.get("code")) for c in item_codes}
    matches = []
    for obs in observations:
        for coding in obs.get("code", {}).get("coding", []):
            if (coding.get("system"), coding.get("code")) in wanted:
                matches.append(obs)
                break
    if not matches:
        return None
    # Recency by effectiveDateTime only; other effective[x] types sort as oldest (v1).
    matches.sort(key=lambda o: o.get("effectiveDateTime", ""), reverse=True)
    best = matches[0]
    if "valueQuantity" in best:
        return best["valueQuantity"]
    if "valueString" in best:
        return best["valueString"]
    if "valueCodeableConcept" in best:
        return best["valueCodeableConcept"].get("text")
    return None


def _initial_expression(item):
    for ext in item.get("extension", []):
        if ext.get("url") == INITIAL_EXPRESSION_URL:
            return (ext.get("valueExpression") or {}).get("expression")
    return None


def _coerce(value, item_type):
    if isinstance(value, dict):
        return value
    if item_type == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if item_type == "decimal":
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if item_type == "boolean":
        return bool(value)
    return str(value)


def _reference(resource):
    if not resource:
        return None
    rtype = resource.get("resourceType")
    rid = resource.get("id")
    if rtype and rid:
        return {"reference": f"{rtype}/{rid}"}
    return None


def _questionnaire_canonical(questionnaire):
    url = questionnaire.get("url")
    if url:
        version = questionnaire.get("version")
        return f"{url}|{version}" if version else url
    qid = questionnaire.get("id")
    return f"Questionnaire/{qid}" if qid else None
