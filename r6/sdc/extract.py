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


#: The resource types commit mode may write on a step-up token alone, with
#: no human confirming the row (r6/sdc/routes.py refuses every other type).
#: Empty on purpose: nothing on the human-gated path calls $extract, so no
#: caller has a legitimate reason to commit here; the form-fill rail writes
#: extracted rows after the person confirms them. Clearing a type is a
#: decision made in this line, with a test (#572, #679).
COMMIT_WITHOUT_CONFIRMATION = frozenset()

#: The elements a definition-based extraction may name, per target type,
#: and exactly the elements _set_path can give a FHIR shape (#666): the
#: list and the setters are one set. Only the types this engine is written
#: to build have a list, and a type without one extracts nothing: a
#: definition's element path is authored by whoever writes the
#: questionnaire, so without this an answer lands in an element the type
#: does not have (Patient.code.text holding an allergen, #681) or in the
#: wrong shape (Patient.telecom as a bare string, #666), and no validator
#: catches either. Adding an element means adding its setter, with a test.
DEFINITION_ELEMENTS = {
    "Patient": frozenset({"name", "birthDate", "gender", "telecom",
                          "address"}),
}


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
    if target_type == "Patient" and subject_ref:
        # #572 part 2A. A response bound to a subject does NOT yield a Patient
        # entry. Each committed form used to POST a new Patient with a fresh
        # uuid, so a tenant accumulated one per submission and every
        # downstream check lost its subject. There is no human-confirmed
        # demographic change to write back: the review page renders
        # demographics read-only, and nothing commits here anyway (#679). A
        # subject-less response still previews a Patient, as it always did;
        # the review rail refuses a reviewed response without a subject, so
        # a form never creates one.
        logger.info("extract: response is bound to a subject; Patient not "
                    "extracted (#572)")
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
        # #681: both halves of a definition are authored by whoever writes
        # the questionnaire, so a consistent type is not enough — the
        # element after it must be one the target has, or the answer lands
        # in an element the type does not have (Patient.code.text holding
        # an allergen) and no validator catches it. Types this engine does
        # not build have no element list and extract nothing.
        element = path.split(".")[1] if "." in path else ""
        if element not in DEFINITION_ELEMENTS.get(target_type, ()):
            logger.warning(
                "extract: item %r not extracted: %s.%s is not an element "
                "this engine builds for %s (#681)",
                item.get("linkId"), target_type, element or "<none>",
                target_type)
            continue
        _value_key, value = _answer_value(item_answers[0])
        if value is None:
            continue
        if not _set_path(resource, path, value):
            logger.warning(
                "extract: item %r not extracted: %s has no shape this engine "
                "builds (#681)", item.get("linkId"), path)
            continue
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
    """Write `value` at an element path like 'Patient.name.family' in the
    shape FHIR gives that element, or return False and write nothing.

    Every element in DEFINITION_ELEMENTS has a shape here; there is no
    generic fallback, because a nested-dict scalar write is wrong for
    every repeating element and structural validation does not see it
    (#666). Repeating elements get one entry per resource — this engine
    builds one Patient from one submission — and `given` is replaced, not
    appended, so a repopulated form does not accumulate names. A
    ContactPoint built from telecom.value alone has no system (cpt-2); the
    engine invents none, and the validator says so.
    """
    parts = dotted_path.split(".")[1:]  # drop resource type
    if len(parts) == 1 and parts[0] in ("birthDate", "gender"):
        resource[parts[0]] = value
        return True
    if len(parts) == 2 and parts[0] == "name" and parts[1] in ("family", "given"):
        entry = resource.setdefault("name", [{}])[0]
        entry[parts[1]] = [value] if parts[1] == "given" else value
        return True
    if len(parts) == 2 and parts[0] == "telecom" and parts[1] in ("system", "value"):
        resource.setdefault("telecom", [{}])[0][parts[1]] = value
        return True
    if len(parts) == 2 and parts[0] == "address" and parts[1] in (
            "line", "city", "state", "postalCode", "country"):
        entry = resource.setdefault("address", [{}])[0]
        entry[parts[1]] = [value] if parts[1] == "line" else value
        return True
    return False


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
