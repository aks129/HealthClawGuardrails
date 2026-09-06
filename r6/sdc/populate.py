"""SDC $populate engine — Questionnaire + subject + content -> QuestionnaireResponse.

Pure function (no DB, no Flask). Supports three SDC population mechanisms:
  - Expression-based: items carrying an initialExpression (FHIRPath). These
    evaluate against a BOUNDED projection of the subject, never the stored
    Patient — see r6/sdc/expressions.py, which is where the allowlist lives
    and where the reasoning for a projection over a filter is written down.
  - Observation-based: items with an item.code (LOINC) matched against
    Observations in the supplied content.
  - List-resource-based: a `repeats: true` group whose leaves carry a
    `definition` naming a supported resource type (MedicationRequest /
    AllergyIntolerance / Condition) emits one repeat of the group per
    matching resource for the subject, resolving leaves at every nesting
    depth inside the group (see _populate_list_group).

THE THREE ARE NOT EXCLUSIVE AND NONE OF THEM DEPENDS ON WHERE THE LEAF SITS.
A leaf inside a list row that carries an initialExpression or an `item.code`
populates by that mechanism, exactly as it would outside one; the row's own
record is tried first and the rest is _resolve_answer's, which is the single
place this rule is implemented.

WHAT COUNTS AS ATTEMPTED, since it is one rule and it is read in two places:
an initialExpression, an `item.code`, or a `definition` on a leaf anywhere
under a `repeats: true` group. ALL THREE CLAUSES, INSIDE A LIST ROW AS WELL —
the engine used to implement the third alone there, so an expression leaf in
an AllergyIntolerance group was neither evaluated nor reported while the same
leaf in an Immunization group was both, which put _LIST_RESOURCE_CONFIG back
into the predicate by the back door (QA review of #584). The third clause
does NOT depend on the type the definition names — a repeating group of Immunizations is an attempted
population this file has no resolver for, so it answers nothing and reports
every leaf, rather than going silent and letting a caller read the silence
as "this patient has no immunizations".

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

#: Why attempted-but-unresolved leaves report, said ONCE PER RESPONSE. ONE
#: sentence for both causes, because the server does not tell the two apart
#: and must not appear to: "the projection withheld it" and "the patient has
#: no email address" read identically here on purpose.
#: `questionnaire_populate` is in the model-facing read tier, and text naming
#: a refusal would have a model tell a patient their record was held back
#: when it is simply empty.
#:
#: ONCE, not once per leaf. This constant is identical for every leaf of
#: every response, so a copy on each issue was pure repetition: it made the
#: response grow with the number of unanswered leaves TIMES a fixed
#: paragraph, and a 29.3KB request came back 3519.6KB over HTTP. The issues
#: now carry the only thing that varies — the linkId — and this sentence is
#: emitted once beside them (r6/sdc/routes.py:_issues_outcome). The CTO
#: ruling on the QA review of #576 chose this over a cap: a cap is a second
#: control to reason about and would silently truncate a legitimate long
#: form, while the amplification was entirely the repetition of a constant.
#:
#: What the caller gets instead is the allowlist, which is public — they can
#: compare it against their own expression without the server answering a
#: question about a patient to do it. (The constant this replaced was called
#: WITHHELD_BY_PROJECTION and said so; it was true for only some of the
#: leaves it was attached to, which is retro pattern 1 living in a name.)
#:
#: The expression is deliberately NOT echoed: a Questionnaire author controls
#: it, and a `where(value='...')` clause is a place to park a literal that
#: would then ride out on an OperationOutcome.
NOT_POPULATED = (
    'not populated: no value resolved. Each accompanying issue names one '
    'such item by linkId. $populate reads only the %patient projection '
    '(name, birthDate, gender, telecom phone/email, address '
    'line/city/state/postalCode) and coded content this server recognises; '
    'anything else is not read.'
)

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
    content_resources: list of resource dicts available for population —
        Observations, and the MedicationRequest / AllergyIntolerance /
        Condition rows the list groups match. NOT the subject: that arrives
        as `subject` above, and putting it here as well left an unredacted
        Patient reachable by anything walking this list (PR #562 review).
    issues: list of {'linkId'} — one entry per leaf OCCURRENCE where
        population was attempted and no value resolved. The reason is the
        same for all of them and is the module constant NOT_POPULATED, said
        once per response by r6/sdc/routes.py:_issues_outcome rather than
        copied onto each entry.
    """
    issues = []
    # Per-request, and threaded exactly like `issues` beside it: the set of
    # linkIds whose expression has already logged a failure. A leaf inside a
    # list group is evaluated once per row, so without this a malformed
    # expression logs a line per record — and the records come from the
    # caller's own inline `content` Bundle. See r6/sdc/expressions.py's
    # `warned`. It is NOT returned: nothing outside this call reads it.
    warned = set()
    # The context carries a BOUNDED projection of `subject`, never the stored
    # Patient (r6/sdc/expressions.py). `subject` itself stays whole here
    # because the list-group and reference paths below match on it; only the
    # FHIRPath environment is projected, which is where a questionnaire
    # author's expression can reach.
    context = build_context(subject=subject)
    observations = [r for r in (content_resources or [])
                    if r.get("resourceType") == "Observation"]

    answer_items = []
    for item in questionnaire.get("item", []):
        answer_items.extend(_populate_item(
            item, subject, context, observations, issues, content_resources,
            warned, in_repeating_group=False))

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


def _populate_item(item, subject, context, observations, issues,
                   content_resources, warned, in_repeating_group):
    """Populate one questionnaire item. Always returns a list of zero or more
    QuestionnaireResponse items (zero or one for ordinary items; zero or many
    for a repeating list-resource group — one per matching resource).

    `in_repeating_group` is True once ANY ancestor group carries
    `repeats: true`. It is what makes a `definition` leaf an attempted
    population — see _resolve_answer's last branch, which is where the rule
    is written down.
    """
    link_id = item.get("linkId")
    item_type = item.get("type")

    if item_type == "group":
        list_resource_type = _list_group_resource_type(item)
        if item.get("repeats") and list_resource_type:
            return _populate_list_group(item, list_resource_type, subject,
                                        context, observations,
                                        content_resources, issues, warned)
        # Ordinary group: recurse, keep the group only if it produced
        # child answers. A `repeats: true` group whose leaves name a type
        # with no resolver lands here too — that is what the flag carries
        # down, so its leaves still count as attempted.
        nested = in_repeating_group or bool(item.get("repeats"))
        children = []
        for child in item.get("item", []):
            children.extend(_populate_item(
                child, subject, context, observations, issues,
                content_resources, warned, nested))
        if not children:
            return []
        return [{"linkId": link_id, "item": children}]

    answer_value, value_key = _resolve_answer(
        item, item_type, context, observations, issues, link_id, warned,
        in_repeating_group)
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


def _populate_list_group(item, resource_type, subject, context, observations,
                         content_resources, issues, warned):
    """Emit one repeat of `item` per matching, currently-relevant resource of
    `resource_type` for `subject`. Returns [] when there are none — an empty
    repeating group, never a default answer for a sibling item (see module
    docstring's safety invariant).

    ROW COUNT EQUALS RECORD COUNT, always. A repeat is emitted for every
    matching resource even when not one of its leaves resolves — an
    unlabelled allergen is a row with no answer and an issue, never a
    dropped row. A patient with three allergies seeing two is a false
    negative on an allergy list, and an allergy section that renders empty
    is one click from someone ticking "no known allergies". Pinned by
    tests/test_populate_lists.py::
    test_three_unlabelled_allergies_still_produce_three_repeats.

    Leaves report through the same mechanism as everything else, and that
    is now literally true rather than nearly: a row's leaves end at
    `_resolve_answer`, the one function that decides what was attempted. A
    leaf with NO mechanism at all — no definition, no expression, no code —
    is left alone there, which is the same exclusion that keeps the
    attestation booleans out of the issue list, and it is why a future
    patient-entered leaf inside a repeating group will not be reported as
    something the server failed to fill.
    """
    config = _LIST_RESOURCE_CONFIG[resource_type]
    subject_ref = _reference(subject)
    resources = [
        r for r in (content_resources or [])
        if r.get("resourceType") == resource_type
        and _references_subject(r, config["subject_field"], subject_ref)
        and config["included"](r)
    ]

    return [_row_with_source(
                {"linkId": item.get("linkId"),
                 "item": _populate_list_children(item.get("item", []),
                                                 resource_type, config, resource,
                                                 context, observations, issues,
                                                 warned)},
                resource_type, resource)
            for resource in resources]


#: A populated repeating-group row names the resource it came from (#572
#: part 2B1). Without it an extraction cannot tell a row that IS a stored
#: resource from a row the person typed, and would duplicate every existing
#: one. Row-level, so it is invisible to the leaves (_resolve_answer), the
#: PDF walk (which reads linkId, text, answer and item) and the answer index
#: (_index_answers reads answers). The review page carries rows verbatim
#: into the reviewed response, so it survives to extraction, where part 2B2
#: skips a marked row and writes only an unsourced one. A row from a
#: resource without an id carries no marker. The marker is caller-visible
#: and forgeable, which is harmless while marked rows are skipped; a future
#: PUT must check the reference exists, in this tenant, for this subject.
POPULATED_ROW_SOURCE_URL = (
    "http://healthclaw.io/fhir/StructureDefinition/populated-row-source")


def _row_with_source(row, resource_type, resource):
    rid = resource.get("id")
    if isinstance(rid, str) and rid:
        row["extension"] = [{
            "url": POPULATED_ROW_SOURCE_URL,
            "valueReference": {"reference": "%s/%s" % (resource_type, rid)},
        }]
    return row


def _populate_list_children(items, resource_type, config, resource, context,
                            observations, issues, warned):
    """Resolve one row's leaves, AT EVERY DEPTH AND BY EVERY MECHANISM, and
    report the ones that resolve nothing.

    THE ROW'S RECORD FIRST, THEN `_resolve_answer` FOR EVERYTHING ELSE. This
    loop used to resolve a leaf by `definition` and by nothing else, and to
    carry its own copy of the reporting decision (`elif
    child.get("definition")`). A leaf carrying an initialExpression or an
    `item.code` inside a MedicationRequest / AllergyIntolerance / Condition
    group was therefore neither evaluated nor reported — an unanswered leaf
    and no issue, which is the silence a caller reads as "this patient has
    no such value". The same leaf inside a repeating group naming a type
    with no resolver WAS evaluated and reported, because that group falls
    through to ordinary recursion: which mechanisms applied depended on
    whether `_LIST_RESOURCE_CONFIG` happened to contain the group's type,
    so deleting the type table from the predicate had moved it here rather
    than removed it (QA review of #584).

    Handing the leaf to `_resolve_answer` leaves ONE implementation of
    "attempted" in this file. A leaf whose definition names an element of
    this row's resource takes the record value; anything else — including a
    definition with no resolver — falls through to the shared predicate,
    which evaluates the other two mechanisms and makes the one reporting
    decision. Adding a fourth resource type still changes only which leaves
    ANSWER.

    NESTED GROUPS ARE WALKED, and that is the fix rather than an
    embellishment. This loop used to visit the repeating group's DIRECT
    children only and test `child.get("definition")`; a child that is itself
    a group carries none, so it was emitted as a bare item and its own
    leaves were never visited at all — no answer, no issue, and no item in
    the response. A stored `reaction.manifestation.text` and the leaf asking
    for it both simply vanished (QA addendum to the review of #576).

    The response mirrors the questionnaire's structure at every depth now,
    which is what makes "every leaf that could carry an answer and does not
    gets an issue" a checkable claim instead of a vacuous one: a leaf that
    is missing from the response is neither answered nor unanswered, so
    dropping it satisfies the biconditional while defeating its purpose.
    Pinned by tests/test_populate_issue_property.py's structural mirror.
    """
    children = []
    for child in items:
        if child.get("type") == "group":
            nested = _populate_list_children(child.get("item", []),
                                             resource_type, config, resource,
                                             context, observations, issues,
                                             warned)
            group_item = {"linkId": child.get("linkId")}
            if nested:
                group_item["item"] = nested
            children.append(group_item)
            continue
        link_id = child.get("linkId")
        child_resource_type, element_path = _parse_definition(
            child.get("definition"))
        resolver = None
        if child_resource_type == resource_type:
            resolver = config["resolvers"].get(element_path)
        value = resolver(resource) if resolver else None
        value_key = _ANSWER_KEY_BY_TYPE.get(child.get("type"), "valueString")
        if value is None:
            # This row's record held nothing for the leaf — or the leaf does
            # not name an element of it at all. The other two mechanisms and
            # the whole reporting decision are _resolve_answer's, including
            # the "nothing was attempted" exclusion the attestation booleans
            # depend on. A leaf carrying both a matching definition and an
            # expression is author-pathological and resolves record-first;
            # nothing in the intake form or the fixtures has one.
            value, value_key = _resolve_answer(
                child, child.get("type"), context, observations, issues,
                link_id, warned, in_repeating_group=True)
        child_item = {"linkId": link_id}
        if value is not None:
            child_item["answer"] = [{value_key: value}]
        children.append(child_item)
    return children


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


def _resolve_answer(item, item_type, context, observations, issues, link_id,
                    warned, in_repeating_group):
    value_key = _ANSWER_KEY_BY_TYPE.get(item_type, "valueString")

    expr = _initial_expression(item)
    if expr:
        # The resource root is `context["patient"]` — the bounded projection,
        # not the stored Patient — so the allowlist applies to the
        # resource-root form (`Patient.name.family`) as well as to `%patient`.
        value = evaluate(expr, context.get("patient"), context,
                         link_id=link_id, warned=warned)
        if value is not None:
            return _coerce(value, item_type), value_key
        _report_unpopulated(issues, link_id)
        return None, value_key

    codes = item.get("code") or []
    if codes:
        # A code-matched item that found no Observation is an attempted
        # population that resolved nothing, exactly like the expression above.
        # The intake Questionnaire has no item.code items, so this is
        # invisible today — another Questionnaire will have them.
        value = _observation_answer(codes, observations)
        if value is not None:
            return value, value_key
        _report_unpopulated(issues, link_id)
        return None, value_key

    # No initialExpression and no code. Two cases, and the difference between
    # them is finding 1 of the QA review of #576.
    #
    # (a) A `definition` leaf inside a repeating group IS an attempted list
    # population — the questionnaire asked for one element of one record per
    # row. Leaves arrive here by both routes: from _populate_list_children,
    # once this row's record has produced no value for them, and from
    # ordinary recursion when the group names a type _LIST_RESOURCE_CONFIG
    # has no resolver for. Reporting lives HERE for both, which is what
    # keeps one rule in one place. They used to
    # return silently: unanswered leaves and ZERO issues — no `issues`
    # parameter on the response at all — which is exactly the state the
    # ruling set out to remove, a caller reading silence as "this patient has
    # no such value". The predicate is deliberately about the
    # QUESTIONNAIRE's structure and nothing else: not the record, and not
    # which resource types this file happens to implement, so adding a
    # fourth type cannot change who reports.
    if in_repeating_group and item.get("definition"):
        _report_unpopulated(issues, link_id)
        return None, value_key

    # (b) NOTHING WAS ATTEMPTED, so nothing is reported. THIS IS THE
    # EXCLUSION AND IT IS LOAD-BEARING —
    # `allergies.no-known-allergies` and `medications.no-current-medications`
    # reach exactly here. They carry no code and no expression precisely so
    # that no mechanism ever touches them (r6/sdc/intake.py). An issue
    # reading "not populated: no value resolved" against `no-known-allergies`
    # tells a model-facing reader that the system tried to determine NKA and
    # could not — which is a hair from inferring it, and inferring it is the
    # non-negotiable. Clause (a) cannot reach them for two independent
    # reasons: they carry no `definition`, and they are siblings of the
    # repeating `<section>.item` group rather than children of it. Pinned by
    # tests/test_populate_lists.py::
    # test_the_attestation_items_never_appear_in_the_issue_list.
    return None, value_key


def _report_unpopulated(issues, link_id):
    """Record that `link_id` was attempted and resolved nothing.

    ONE FIELD, and it is the only thing that varies between two of these.
    The reason is NOT_POPULATED — the same constant for every leaf of every
    response — so a copy per issue made the response grow with the number of
    unanswered leaves TIMES a fixed paragraph, measured at 3519.6KB back from
    a 29.3KB request. r6/sdc/routes.py:_issues_outcome says the sentence once
    and gives each issue its linkId.

    UNCONDITIONAL, and that is the design rather than an omission. The
    response already says which leaves have no answer — `_populate_item`
    attaches `answer` only on success — so an issue per unanswered attempted
    leaf is a redundant encoding of what the caller is holding, and carries
    no information about the patient BY CONSTRUCTION. There is nothing here
    to verify does not leak.

    Do not add a condition. Every predicate that has stood here was a
    question about the record or about the expression, and both shapes have
    already failed in this file: one evaluated the caller's expression
    against the unbounded Patient (a one-bit oracle — eleven HTTP requests
    recovered a stored identifier), and its replacement asked a constant
    probe instead, which was safe but reported only six of twelve withheld
    expressions, so the other six came back silent and a caller reads silence
    as "this patient has no such value". Both are `docs/2026-08-02-retro.md`
    pattern 1. Pinned by tests/test_populate_issue_property.py, which asserts
    the biconditional per item; any classifier reddens on the first leaf it
    silences.
    """
    issues.append({"linkId": link_id})


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
