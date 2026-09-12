"""SDC $extract engine — QuestionnaireResponse -> transaction Bundle.

Pure function. Supports two SDC extraction mechanisms:
  - Observation-based: items flagged observationExtract + item.code -> Observation.
  - Definition-based: root definitionExtract names the target resource type;
    items carry `definition` (StructureDefinition#element.path) -> element values.

Out of scope (v1): template-based and StructureMap-based extraction.
"""

import logging

logger = logging.getLogger(__name__)

OBSERVATION_EXTRACT_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-observationExtract"
)
DEFINITION_EXTRACT_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-definitionExtract"
)


def extract_resources(questionnaire_response, questionnaire):
    """Return a FHIR transaction Bundle of resources extracted from `qr`."""
    subject_ref = questionnaire_response.get("subject")
    answers = _index_answers(questionnaire_response.get("item", []))

    entries = []
    entries.extend(_extract_observations(questionnaire, answers, subject_ref))
    entries.extend(_extract_by_definition(questionnaire, answers, subject_ref))

    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def _index_answers(items, acc=None):
    """Flatten QR items into {linkId: [answer, ...]} (recurses groups)."""
    acc = acc if acc is not None else {}
    for item in items:
        if "answer" in item:
            acc[item["linkId"]] = item["answer"]
        if "item" in item:
            _index_answers(item["item"], acc)
    return acc


def _extract_observations(questionnaire, answers, subject_ref):
    entries = []
    root_flag = _has_extension(questionnaire, OBSERVATION_EXTRACT_URL)
    for item in _walk_items(questionnaire.get("item", [])):
        if not (root_flag or _has_extension(item, OBSERVATION_EXTRACT_URL)):
            continue
        codes = item.get("code") or []
        if not codes:
            continue
        for answer in answers.get(item.get("linkId"), []):
            obs = {
                "resourceType": "Observation",
                "status": "final",
                "code": {"coding": codes},
            }
            if subject_ref:
                obs["subject"] = subject_ref
            value_key, value = _answer_value(answer)
            if value_key:
                obs[value_key] = value
            entries.append(_post_entry(obs))
    return entries


def _extract_by_definition(questionnaire, answers, subject_ref):
    target_type = _extension_value(questionnaire, DEFINITION_EXTRACT_URL,
                                   "valueCode")
    if not target_type:
        return []
    resource = {"resourceType": target_type}
    populated = False
    for item in _walk_items(questionnaire.get("item", [])):
        definition = item.get("definition")
        if not definition or "#" not in definition:
            continue
        path = definition.split("#", 1)[1]  # e.g. Patient.name.family
        item_answers = answers.get(item.get("linkId"), [])
        if not item_answers:
            continue
        # #572: a definition names its own resource type. This engine builds
        # ONE target type, so an answer declared for another type (the intake
        # form's allergen, AllergyIntolerance#AllergyIntolerance.code.text)
        # must not be written into the target as if it were an element of
        # it: that produced Patient.code.text holding an allergen name, an
        # element Patient does not have and no validator catches. Until the
        # engine builds those types too, the answer is dropped and the drop
        # is said out loud, naming the item and the types, never the answer.
        # A definition names its resource type twice: in the StructureDefinition
        # URL before "#" and as the first segment of the element path after
        # it. Both must be the type this questionnaire extracts. The QA pass
        # on #664 showed why one is not enough: `AllergyIntolerance#Patient.
        # name.given` passed a path-only check and landed the allergen answer
        # in Patient.name.given, and a Questionnaire is a stored resource
        # that can arrive through the ordinary ingest paths, not only the
        # intake form this engine was written for.
        defined_url_type = _definition_url_type(definition)
        defined_path_type = path.split(".", 1)[0]
        if defined_path_type != target_type or (
                defined_url_type and defined_url_type != target_type):
            # %r for the linkId, as the handler in expressions.py does: it is
            # caller-supplied questionnaire structure, and %r is what escapes
            # a newline that would forge a log line.
            logger.warning(
                "extract: item %r not extracted: its definition targets %s "
                "(url) / %s (path) and this questionnaire extracts %s (#572)",
                item.get("linkId"), defined_url_type or "none",
                defined_path_type, target_type)
            continue
        _value_key, value = _answer_value(item_answers[0])
        if value is None:
            continue
        _set_path(resource, path, value)
        populated = True
    if not populated:
        return []
    return [_post_entry(resource)]


_HL7_BASE_SD = "http://hl7.org/fhir/StructureDefinition/"


def _definition_url_type(definition):
    """The base resource type the StructureDefinition URL before "#" names,
    when that can be read off the URL: the last segment under the HL7 base
    namespace (http://hl7.org/fhir/StructureDefinition/Patient -> Patient),
    or a bare type name ("Patient"). Any other URL is a profile whose base
    type is not in its name (http://example.org/SD, us-core-patient), and
    cannot be checked without resolving the profile: "" means unknown, and
    the element path's own type is what the check has."""
    url = definition.split("#", 1)[0].strip()
    if not url:
        return ""
    if url.startswith(_HL7_BASE_SD):
        return url[len(_HL7_BASE_SD):].strip("/")
    if "/" not in url and ":" not in url:
        return url
    return ""


def _set_path(resource, dotted_path, value):
    """Set a value at an element path like 'Patient.name.family'.

    The leading resource-type segment is dropped.

    v1 scope: only `name.*` (HumanName index 0; `given` appends) and
    `birthDate` are mapped with correct FHIR cardinality. Any other path
    falls through to a generic nested-dict scalar write — which is WRONG for
    repeating elements (e.g. telecom, address, identifier are arrays). Such
    paths are not part of the seeded-demo v1 surface; extending to arbitrary
    US Core element paths is a future phase. Structural-only downstream
    validation will NOT catch a malformed shape here.
    """
    parts = dotted_path.split(".")[1:]  # drop resource type
    if not parts:
        return
    if parts == ["birthDate"]:
        resource["birthDate"] = value
        return
    if parts[:1] == ["name"] and len(parts) == 2:
        names = resource.setdefault("name", [{}])
        field = parts[1]
        if field == "given":
            names[0].setdefault("given", []).append(value)
        else:
            names[0][field] = value
        return
    # Generic fallback: nested dict path, scalar leaf.
    cursor = resource
    for segment in parts[:-1]:
        cursor = cursor.setdefault(segment, {})
    cursor[parts[-1]] = value


def _answer_value(answer):
    for key, value in answer.items():
        if key.startswith("value"):
            return key, value
    return None, None


def _walk_items(items):
    for item in items:
        yield item
        if "item" in item:
            yield from _walk_items(item["item"])


def _has_extension(node, url):
    return any(ext.get("url") == url for ext in node.get("extension", []))


def _extension_value(node, url, value_key):
    for ext in node.get("extension", []):
        if ext.get("url") == url:
            return ext.get(value_key)
    return None


def _post_entry(resource):
    return {"resource": resource,
            "request": {"method": "POST",
                        "url": resource["resourceType"]}}
