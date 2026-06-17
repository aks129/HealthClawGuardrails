"""SDC $populate engine — Questionnaire + subject + content -> QuestionnaireResponse.

Pure function (no DB, no Flask). Supports two SDC population mechanisms:
  - Expression-based: items carrying an initialExpression (FHIRPath).
  - Observation-based: items with an item.code (LOINC) matched against
    Observations in the supplied content.

Out of scope (v1): StructureMap-based and CQL populate.
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
        populated = _populate_item(item, subject, context, observations, issues)
        if populated is not None:
            answer_items.append(populated)

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


def _populate_item(item, subject, context, observations, issues):
    link_id = item.get("linkId")
    item_type = item.get("type")

    # Group: recurse, keep the group only if it produced child answers.
    if item_type == "group":
        children = []
        for child in item.get("item", []):
            populated = _populate_item(child, subject, context,
                                       observations, issues)
            if populated is not None:
                children.append(populated)
        if not children:
            return None
        return {"linkId": link_id, "item": children}

    answer_value, value_key = _resolve_answer(
        item, item_type, context, observations, issues, link_id)
    # Leaf items are always emitted so the response mirrors the questionnaire's
    # structure; the answer array is attached only when a value resolved.
    answer_item = {"linkId": link_id}
    if answer_value is not None:
        answer_item["answer"] = [{value_key: answer_value}]
    return answer_item


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
    if item_type in ("integer",):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if item_type in ("decimal",):
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
