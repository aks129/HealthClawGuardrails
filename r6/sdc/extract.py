"""SDC $extract engine — QuestionnaireResponse -> transaction Bundle.

Pure function. Supports two SDC extraction mechanisms:
  - Observation-based: items flagged observationExtract + item.code -> Observation.
  - Definition-based: root definitionExtract names the target resource type;
    items carry `definition` (StructureDefinition#element.path) -> element values.

Out of scope (v1): template-based and StructureMap-based extraction.
"""

import logging
import uuid

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
    entries.extend(_extract_rows(questionnaire, questionnaire_response,
                                 subject_ref))

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
        # demographics read-only, and _set_path's shapes for telecom and
        # address are not safe to write over a real record (#666). A
        # subject-less response still creates a Patient, as it always did;
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


# ---------------------------------------------------------------------------
# Repeating-group rows -> one clinical resource each (#572 part 2B2)
# ---------------------------------------------------------------------------

#: The types the form-fill rail commits after human confirmation. $extract
#: builds them for a dryRun preview and for the executor; commit mode on the
#: raw endpoint refuses a bundle carrying them (r6/sdc/routes.py), because
#: nothing on the human-gated path calls $extract and a step-up token alone
#: must not write clinical rows.
RAIL_ONLY_TYPES = frozenset({"AllergyIntolerance", "Condition",
                             "MedicationRequest"})

_ROW_SUBJECT_FIELD = {"AllergyIntolerance": "patient",
                      "MedicationRequest": "subject",
                      "Condition": "subject"}

_ALLERGY_CLINICAL = "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical"
_ALLERGY_VERIFICATION = "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification"


def _row_defaults(resource_type):
    """The minimum truthful statuses the validator demands. An
    AllergyIntolerance a person typed is active and unconfirmed; a
    MedicationRequest they reported is an active plan, patient-reported."""
    if resource_type == "AllergyIntolerance":
        return {"clinicalStatus": {"coding": [{"system": _ALLERGY_CLINICAL,
                                               "code": "active"}]},
                "verificationStatus": {"coding": [{"system": _ALLERGY_VERIFICATION,
                                                   "code": "unconfirmed"}]}}
    if resource_type == "MedicationRequest":
        return {"status": "active", "intent": "plan", "reportedBoolean": True}
    return {}


def _set_code_text(resource, value):
    resource.setdefault("code", {})["text"] = value


def _set_reaction_text(resource, value):
    reaction = resource.setdefault("reaction", [{}])[0]
    reaction.setdefault("manifestation", [{}])[0]["text"] = value


def _set_medication_text(resource, value):
    resource.setdefault("medicationCodeableConcept", {})["text"] = value


def _set_dosage_text(resource, value):
    resource.setdefault("dosageInstruction", [{}])[0]["text"] = value


#: Fixed-cardinality setters only. A path outside this table is not written:
#: the generic nested-dict write in _set_path is wrong for repeating elements
#: (#666), and a clinical row must never carry a shape the validator cannot
#: see.
_ROW_SETTERS = {
    "AllergyIntolerance": {"code.text": _set_code_text,
                           "reaction.manifestation.text": _set_reaction_text},
    "MedicationRequest": {"medicationCodeableConcept.text": _set_medication_text,
                          "dosageInstruction.text": _set_dosage_text},
    "Condition": {"code.text": _set_code_text},
}


def _definition_type_and_path(definition):
    if not definition or "#" not in definition:
        return None, None
    path = definition.split("#", 1)[1]
    if "." not in path:
        return None, None
    resource_type, element = path.split(".", 1)
    url_type = _definition_url_type(definition)
    if url_type and url_type != resource_type:
        return None, None   # the #664 rule: the two halves must agree
    return resource_type, element


def _row_groups(questionnaire):
    """{group linkId: (resource type, {child linkId: element path})} for
    every repeating group whose children are defined on a rail-only type."""
    groups = {}
    for item in _walk_items(questionnaire.get("item", [])):
        if not item.get("repeats") or not item.get("item"):
            continue
        leaves = {}
        types = set()
        for child in item["item"]:
            resource_type, element = _definition_type_and_path(
                child.get("definition"))
            if resource_type in RAIL_ONLY_TYPES:
                types.add(resource_type)
                leaves[child.get("linkId")] = element
        if len(types) == 1 and leaves:
            groups[item.get("linkId")] = (types.pop(), leaves)
    return groups


def _extract_rows(questionnaire, questionnaire_response, subject_ref):
    """One resource per UNSOURCED repeating-group row, walked structurally.

    _index_answers is last-wins on a repeated linkId, so rows are read from
    the response tree itself. A row the populate engine sourced from a
    stored resource carries POPULATED_ROW_SOURCE_URL (#572 part 2B1) and IS
    the record: confirming it writes nothing. The no-known-allergies
    attestation has no definition and is never a row; an empty group yields
    nothing; nothing here synthesizes "no known allergies". A row with no
    subject to bind to yields nothing, said out loud.
    """
    # Local import: the marker's owner is the populate engine and this keeps
    # the dependency one-way.
    from r6.sdc.populate import POPULATED_ROW_SOURCE_URL

    groups = _row_groups(questionnaire)
    if not groups:
        return []
    entries = []
    for row in _walk_items(questionnaire_response.get("item", [])):
        group = groups.get(row.get("linkId"))
        if group is None or "item" not in row:
            continue
        if _has_extension(row, POPULATED_ROW_SOURCE_URL):
            continue
        resource_type, leaves = group
        if not subject_ref:
            logger.warning("extract: a %s row has no subject to bind to; "
                           "not extracted (#572)", resource_type)
            continue
        resource = {"resourceType": resource_type}
        resource.update(_row_defaults(resource_type))
        written = False
        for child in row.get("item", []):
            element = leaves.get(child.get("linkId"))
            setter = _ROW_SETTERS[resource_type].get(element)
            answers = child.get("answer") or []
            if setter is None or not answers:
                continue
            _value_key, value = _answer_value(answers[0])
            if value is None:
                continue
            setter(resource, value)
            written = True
        if not written:
            continue
        resource[_ROW_SUBJECT_FIELD[resource_type]] = subject_ref
        entry = _post_entry(resource)
        entry["fullUrl"] = "urn:uuid:%s" % uuid.uuid4()
        entries.append(entry)
    return entries


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
