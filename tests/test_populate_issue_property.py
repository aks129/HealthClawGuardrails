"""The $populate issue list carries zero bits about the patient.

THE PROPERTY, stated once: for every leaf where population was ATTEMPTED,

    an answer is present  <=>  no issue names that occurrence

so the issue list is a redundant re-encoding of something the caller is
already holding — `_populate_item` attaches `answer` only when a value
resolved, so *which* leaves came back empty is in the response either way.
Nothing about the record can reach the caller through a channel that says
only what the caller can already count. That is the whole reason the issue
is emitted unconditionally rather than classified.

It replaces a classifier that decided whether to report by asking a question
*about the record*. The first cut evaluated the caller's own expression
against the unbounded Patient and reported whether it came back non-empty —
a one-bit oracle that walked a stored identifier out over eleven HTTP
requests (QA review of PR #562). The second cut asked the same question of a
constant probe, which closed the leak but answered "no issue" for six of the
ruling's twelve withheld expressions, because it fired only when the
caller's `where()` filter happened to match the probe's placeholder text.
Both are `docs/2026-08-02-retro.md` pattern 1: a control that looks like "we
tell you when we refused" and does something else. Neither shape can pass
this file — a classifier that silences any attempted leaf changes a count
here, and a classifier driven by the record changes it *per patient*, which
is what `test_the_issue_set_is_identical_for_every_patient` measures.

REPEATS: the biconditional is stated as counts, not booleans. One allergy
row labelled and one not is one answered occurrence and one issue on the
SAME linkId, so `answer present <=> no issue` only reads correctly per
occurrence. Counter equality says exactly that.

ATTEMPTED, and why the predicate is read from the QUESTIONNAIRE here rather
than imported from the engine: a leaf with no population mechanism at all
must never be reported. `allergies.no-known-allergies` and
`medications.no-current-medications` carry no `code` and no
initialExpression precisely so nothing ever touches them, and an issue
reading "not populated: no value resolved" against them tells a
model-facing reader that the system tried to determine "no known allergies"
and failed — one step from inferring it. Deriving the predicate from the
questionnaire's own structure keeps this test independent of how populate.py
decides.
"""

import json
from collections import Counter

from r6.sdc.intake import intake_questionnaire
from r6.sdc.populate import NOT_POPULATED, populate_questionnaire
from r6.sdc.routes import _issues_outcome

INITIAL_EXPR_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-initialExpression"
)

SNOMED = "http://snomed.info/sct"


# ---------------------------------------------------------------------------
# The matrix: patients x expressions
# ---------------------------------------------------------------------------

#: Full demographics — every allowlisted path resolves.
FULL_PATIENT = {
    "resourceType": "Patient",
    "id": "p1",
    "name": [{"given": ["Ada"], "family": "Lovelace"}],
    "birthDate": "1815-12-10",
    "gender": "female",
    "identifier": [{"system": "http://hl7.org/fhir/sid/us-ssn",
                    "value": "123-45-6789"}],
    "photo": [{"url": "https://example.org/ada.jpg"}],
    "maritalStatus": {"text": "Married"},
    "contact": [{"name": {"family": "Byron"}, "gender": "male"}],
    "deceasedDateTime": "2026-01-01",
    "telecom": [{"system": "phone", "value": "617-555-0198"},
                {"system": "email", "value": "ada@example.org"},
                {"system": "sms", "value": "617-555-0199"}],
    "address": [{"line": ["123 Clinical Ave"], "city": "Boston",
                 "state": "MA", "postalCode": "02101"}],
}

#: One given name and nothing else. Every other allowlisted path is genuinely
#: absent rather than withheld — the case the old text got wrong out loud.
SPARSE_PATIENT = {
    "resourceType": "Patient",
    "id": "p1",
    "name": [{"given": ["Ada"]}],
}

#: No demographics at all, and a different withheld identifier, so a
#: record-driven classifier would answer differently here than on FULL.
BARE_PATIENT = {
    "resourceType": "Patient",
    "id": "p1",
    "identifier": [{"system": "http://hl7.org/fhir/sid/us-ssn",
                    "value": "999-99-9999"}],
}

PATIENT_MATRIX = {
    "full": FULL_PATIENT,
    "sparse": SPARSE_PATIENT,
    "bare": BARE_PATIENT,
}

#: linkId -> expression. The CTO ruling's own twelve-row live matrix, which
#: is where the probe was measured and found to report six of them. The
#: `oracle-*` rows are the `where()` shapes that walked an identifier out;
#: they are the ordinary way an author writes a filter, which is why the
#: probe's placeholder-matching gap was not an edge case.
WITHHELD_EXPRESSIONS = {
    # D10's three named negatives.
    "mrn": "%patient.identifier.value",
    "photo": "%patient.photo.url",
    "obs-text": "%resources.where(resourceType='Observation').code.text",
    # Withheld, and reported by the probe.
    "marital": "%patient.maritalStatus.text",
    "sms": "%patient.telecom.where(system='sms').value",
    "mrn-first": "%patient.identifier.first().value",
    # Withheld, and SILENT under the probe. Each of these is a row the
    # classifier answered False for while the projection withheld the value.
    "oracle-1": "%patient.identifier.value.where($this.startsWith('1'))",
    "oracle-9": "%patient.identifier.value.where($this.startsWith('9'))",
    "contact-gender": "%patient.contact.gender",
    "deceased": "%patient.deceasedDateTime",
    "name-period": "%patient.name.period.start",
}

#: The ruling's twelfth row, held apart because it is not a silence at all.
#: `combine` unions a withheld path with an ALLOWLISTED one, so on a patient
#: who has a gender it resolves — to the gender, and to nothing else. The
#: item is ANSWERED, which is why it reported no issue under the probe and
#: reports none now. It varies with the record exactly as far as the answer
#: does, which is the allowlist working, so it cannot ride in the invariance
#: test below.
COMBINE_EXPRESSION = {
    "oracle-combine": "%patient.identifier.value.combine(%patient.gender)",
}

#: An ALLOWLISTED path the sparse fixture patient genuinely lacks. It is not
#: withheld and never was; under the ruling it is reported in exactly the
#: same words, which is the point — the text claims nothing about why.
ABSENT_BUT_ALLOWLISTED = {
    "email": "%patient.telecom.where(system='email').value",
}

ALLOWLISTED = {
    "given": "%patient.name.given.first()",
    "family": "%patient.name.family",
    "dob": "%patient.birthDate",
}


def _expr_item(link_id, expression, item_type="string"):
    return {
        "linkId": link_id, "type": item_type,
        "extension": [{
            "url": INITIAL_EXPR_URL,
            "valueExpression": {"language": "text/fhirpath",
                                "expression": expression},
        }],
    }


def _expression_questionnaire(expressions):
    return {"resourceType": "Questionnaire", "id": "property-q",
            "status": "active",
            "item": [_expr_item(link_id, expr)
                     for link_id, expr in expressions.items()]}


# ---------------------------------------------------------------------------
# The property, and the two halves it is built from
# ---------------------------------------------------------------------------

def _attempted_link_ids(questionnaire, in_repeating_group=False):
    """linkIds where the questionnaire asks populate to resolve something.

    Read from the Questionnaire, not from populate.py, so this test still
    means something if the engine's own predicate changes. Three mechanisms,
    matching the module docstring of r6/sdc/populate.py:

      - an initialExpression (FHIRPath against the %patient projection),
      - an `item.code` (Observation matching),
      - a `definition` on a leaf ANYWHERE under a `repeats: true` group.

    `definition` alone is deliberately not enough: every demographics leaf on
    the intake form carries one for `$extract`'s benefit and populates by
    expression, and a definition-bearing leaf outside a repeating group is
    not an attempted population.

    THE THIRD CLAUSE NAMES NO RESOURCE TYPE, and that is finding 1 of the QA
    review of #576 fixed at its root. This helper used to carry
    `LIST_POPULATED_TYPES`, a hand-copied literal of
    r6/sdc/populate.py's `_LIST_RESOURCE_CONFIG` keys, because the engine
    attempted a repeating group only when its leaves named MedicationRequest
    / AllergyIntolerance / Condition and went silent on every other type.
    Two files restating one type table is a divergence waiting to happen —
    and it had already happened, in the other direction, which is how a
    shape with unanswered leaves and zero issues passed this file. The
    engine's predicate is now about questionnaire structure only, so there
    is no type table left to mirror: a fourth resource type changes which
    leaves ANSWER, never which leaves report.
    """
    found = set()
    for item in questionnaire.get("item", []):
        if item.get("type") == "group":
            found |= _attempted_link_ids(
                item, in_repeating_group or bool(item.get("repeats")))
            continue
        if (_has_initial_expression(item) or item.get("code")
                or (in_repeating_group and item.get("definition"))):
            found.add(item["linkId"])
    return found


def _has_initial_expression(item):
    return any(ext.get("url") == INITIAL_EXPR_URL
               for ext in item.get("extension", []))


def _leaf_occurrences(items):
    """Every leaf item in the QUESTIONNAIRERESPONSE, repeats included.

    One entry per occurrence, not per linkId: two allergy rows are two
    occurrences of `allergies.item.allergen`.
    """
    for item in items:
        if "item" in item:
            yield from _leaf_occurrences(item["item"])
        else:
            yield item


def _response_items(items):
    """Every item in the response, groups included, one per occurrence."""
    for item in items:
        yield item
        if "item" in item:
            yield from _response_items(item["item"])


def _questionnaire_leaf_link_ids(item):
    """Every leaf linkId under a questionnaire item, at any depth.

    A group with no children counts as a leaf, which is how
    `_leaf_occurrences` reads the response side — the two walks have to
    agree on the word or the comparison below means nothing.
    """
    children = item.get("item") or []
    if not children:
        return [item.get("linkId")]
    out = []
    for child in children:
        out.extend(_questionnaire_leaf_link_ids(child))
    return out


def _repeating_group_leaves(questionnaire):
    """linkId -> the leaves every emitted repeat of that group must carry."""
    out = {}

    def walk(item):
        for child in item.get("item", []):
            if child.get("type") != "group":
                continue
            if child.get("repeats") and child.get("item"):
                out[child["linkId"]] = sorted(
                    _questionnaire_leaf_link_ids(child))
            walk(child)

    walk(questionnaire)
    return out


def _assert_zero_bits(questionnaire, qr, issues, label):
    """THE PROPERTY. Per linkId: unanswered attempted occurrences == issues.

    MUTATION: silence any attempted leaf (re-add a classifier, of any shape)
    -> the expected count exceeds the actual and this goes red naming the
    linkId. Emit for an unattempted leaf -> the second assertion goes red.

    THE STRUCTURAL MIRROR FIRST, because without it the biconditional can be
    satisfied by DROPPING a leaf. A leaf that is absent from the response is
    neither answered nor unanswered: it contributes nothing to either side
    of the count, so the property holds vacuously and green means nothing.
    That is not hypothetical — `_populate_list_group` used to lose every
    leaf inside a nested group and this file passed (QA addendum to the
    review of #576). So every emitted repeat of a repeating group must carry
    exactly the leaves the questionnaire puts in it, at every depth.
    """
    for wanted_group, wanted_leaves in _repeating_group_leaves(
            questionnaire).items():
        for row in _response_items(qr.get("item", [])):
            if row.get("linkId") != wanted_group:
                continue
            emitted = sorted(leaf.get("linkId")
                             for leaf in _leaf_occurrences([row]))
            assert emitted == wanted_leaves, (
                f"[{label}] a repeat of {wanted_group} does not mirror the "
                f"questionnaire: missing="
                f"{sorted(set(wanted_leaves) - set(emitted))}, "
                f"unexpected={sorted(set(emitted) - set(wanted_leaves))}")

    attempted = _attempted_link_ids(questionnaire)
    expected = Counter()
    unattempted_answered = []
    for leaf in _leaf_occurrences(qr.get("item", [])):
        link_id = leaf.get("linkId")
        if link_id in attempted:
            if "answer" not in leaf:
                expected[link_id] += 1
        elif "answer" in leaf:
            unattempted_answered.append(link_id)

    actual = Counter(issue["linkId"] for issue in issues)
    assert actual == expected, (
        f"[{label}] the issue list is not the unanswered-leaf list: "
        f"only-in-issues={actual - expected}, only-unanswered={expected - actual}")

    # The other half of "attempted": nothing without a population mechanism
    # may be answered OR reported. This is the NKA guard restated as a
    # property rather than a spot check.
    unattempted_named = set(actual) - attempted
    assert not unattempted_named, (
        f"[{label}] issues name leaves with no population mechanism: "
        f"{sorted(unattempted_named)}")
    assert not unattempted_answered, (
        f"[{label}] leaves with no population mechanism were answered: "
        f"{sorted(unattempted_answered)}")

    # Every issue carries ONLY the linkId. The reason is the same constant
    # for all of them, said once per response by
    # r6/sdc/routes.py:_issues_outcome — see
    # test_the_explanation_is_said_once_however_many_leaves_are_unanswered.
    # A copy of it per issue is what made a 29.3KB request come back
    # 3519.6KB.
    for issue in issues:
        assert set(issue) == {"linkId"}, issue


# ---------------------------------------------------------------------------
# 1. The property over the matrix
# ---------------------------------------------------------------------------

def test_answer_present_iff_no_issue_across_patients_and_expressions():
    """Every patient x every expression class, per item.

    This is the assertion the ruling asked for. It is implementation-
    independent: it never imports the engine's own notion of "withheld", only
    the questionnaire's structure and the response's own answers.
    """
    expressions = dict(WITHHELD_EXPRESSIONS)
    expressions.update(COMBINE_EXPRESSION)
    expressions.update(ABSENT_BUT_ALLOWLISTED)
    expressions.update(ALLOWLISTED)
    q = _expression_questionnaire(expressions)

    for label, patient in PATIENT_MATRIX.items():
        qr, issues = populate_questionnaire(q, patient, [patient])
        _assert_zero_bits(q, qr, issues, label)


def test_the_intake_form_holds_the_property_on_every_patient():
    """The real Questionnaire, including its list groups and its two
    attestation booleans, over the same patient matrix."""
    q = intake_questionnaire()
    content_shapes = {
        "no clinical content": [],
        "one unlabelled allergy": [{
            "resourceType": "AllergyIntolerance", "id": "a1",
            "patient": {"reference": "Patient/p1"},
            "code": {"coding": [{"system": SNOMED, "code": "91936005"}]},
        }],
    }
    for label, patient in PATIENT_MATRIX.items():
        for shape, extra in content_shapes.items():
            qr, issues = populate_questionnaire(
                q, patient, [patient] + extra)
            _assert_zero_bits(q, qr, issues, f"{label} / {shape}")


# ---------------------------------------------------------------------------
# 2. Zero bits, stated as invariance rather than as a count
# ---------------------------------------------------------------------------

def test_the_issue_set_is_identical_for_every_patient():
    """A questionnaire of withheld expressions reports the SAME linkIds for
    every patient. Nothing in the record can move this list.

    The count property above is the general statement; this is the one that
    reads as a security claim. Any classifier that consults the record —
    however narrow its output — differs between FULL (which HAS an
    identifier, a photo, a maritalStatus and an sms telecom) and BARE (which
    has only the identifier), and lands here first.

    MUTATION: report only when the expression resolves against the real
    subject -> FULL names all eleven and SPARSE names none.
    """
    q = _expression_questionnaire(WITHHELD_EXPRESSIONS)

    named = {}
    for label, patient in PATIENT_MATRIX.items():
        qr, issues = populate_questionnaire(q, patient, [patient])
        named[label] = sorted(issue["linkId"] for issue in issues)
        assert not [leaf for leaf in _leaf_occurrences(qr["item"])
                    if "answer" in leaf], "a withheld path produced an answer"

    assert named["full"] == sorted(WITHHELD_EXPRESSIONS), (
        "a withheld expression came back silent: the caller reads that as "
        "'this patient has no such value'")
    assert len(set(map(tuple, named.values()))) == 1, (
        f"the issue list varies with the record: {named}")


def test_a_right_guess_and_a_wrong_guess_are_indistinguishable():
    """The oracle, restated at the engine.

    `test_the_withheld_issue_cannot_be_used_to_guess_the_withheld_value` in
    tests/test_sdc_populate_bounded.py walks this over HTTP, which is where
    it matters; this is the same property one layer down, where it is cheap
    enough to run over the whole alphabet.
    """
    secret = FULL_PATIENT["identifier"][0]["value"]
    alphabet = sorted(set(secret))
    q = _expression_questionnaire({
        f"guess-{n}": ("%patient.identifier.value.where($this.startsWith('"
                       + ch + "'))")
        for n, ch in enumerate(alphabet)
    })

    _qr, issues = populate_questionnaire(q, FULL_PATIENT, [FULL_PATIENT])

    named = sorted(issue["linkId"] for issue in issues)
    assert named == sorted(f"guess-{n}" for n in range(len(alphabet))), (
        f"the issue list singled out a guess: {named}")


# ---------------------------------------------------------------------------
# 3. What the issue says
# ---------------------------------------------------------------------------

def test_the_issue_text_is_true_whether_the_value_was_withheld_or_absent():
    """One sentence for both causes, claiming nothing about the patient.

    `questionnaire_populate` is in the model-facing read tier. Text saying
    the record was withheld would have a model tell a patient their data was
    held back when they simply have no email address — so the text states
    what did not happen (no value resolved) and hands over the allowlist for
    the caller to compare against their own expression.

    Since the explanation became one constant said once per response, the
    two causes are indistinguishable BY CONSTRUCTION rather than by matching
    strings: the only thing an issue carries is the linkId the caller wrote.
    So the assertion is that the two issue payloads differ in nothing but
    that linkId, and the words are checked once, on the constant.
    """
    withheld_q = _expression_questionnaire({"mrn": WITHHELD_EXPRESSIONS["mrn"]})
    absent_q = _expression_questionnaire(ABSENT_BUT_ALLOWLISTED)

    _qr, withheld_issues = populate_questionnaire(
        withheld_q, SPARSE_PATIENT, [SPARSE_PATIENT])
    _qr, absent_issues = populate_questionnaire(
        absent_q, SPARSE_PATIENT, [SPARSE_PATIENT])

    assert withheld_issues == [{"linkId": "mrn"}]
    assert absent_issues == [{"linkId": "email"}]

    # The allowlist is public and is the actionable half.
    for element in ("name", "birthDate", "gender", "telecom", "address"):
        assert element in NOT_POPULATED, element
    # Nothing that reads as a claim about this patient or this request.
    for forbidden in ("withheld", "refused", "denied", "not permitted",
                      "does not have", "no such"):
        assert forbidden not in NOT_POPULATED.lower(), forbidden


def test_the_expression_text_is_never_echoed_back():
    """A `where()` clause is a place a questionnaire author parks a literal;
    the OperationOutcome leaves the boundary. Pre-existing property of the
    fixed sentence, pinned here because the retext could have lost it.

    Asserted on the RENDERED OperationOutcome, not on the engine's issue
    list, because that is what crosses the boundary — and because the
    rendering is now the only place any text is chosen.
    """
    expression = "%patient.identifier.value.where($this='a-planted-literal')"
    q = _expression_questionnaire({"planted": expression})

    _qr, issues = populate_questionnaire(q, FULL_PATIENT, [FULL_PATIENT])

    assert issues == [{"linkId": "planted"}]
    rendered = json.dumps(_issues_outcome(issues))
    assert "a-planted-literal" not in rendered
    assert expression not in rendered


# ---------------------------------------------------------------------------
# 4. The counts the ruling states out loud
# ---------------------------------------------------------------------------

def test_full_demographics_populate_the_intake_form_with_zero_issues():
    """The ruling's noise measurement: nine initialExpression items, all
    nine answer, no issues. Unconditional emission is only tolerable if an
    ordinary form on an ordinary patient is quiet."""
    q = intake_questionnaire()

    qr, issues = populate_questionnaire(q, FULL_PATIENT, [FULL_PATIENT])

    answered = [leaf["linkId"] for leaf in _leaf_occurrences(qr["item"])
                if "answer" in leaf]
    assert len(answered) == 9, answered
    assert issues == []


def test_one_absent_demographic_gives_exactly_one_issue():
    """And the other half of the measurement, which is what makes the first
    half mean something."""
    patient = {k: v for k, v in FULL_PATIENT.items() if k != "address"}
    q = intake_questionnaire()

    _qr, issues = populate_questionnaire(
        q, patient, [patient])

    # Four address leaves lose their answer, so pull one back to isolate it.
    patient_with_city = dict(patient, address=[{"city": "Boston",
                                                "line": ["123 Clinical Ave"],
                                                "state": "MA"}])
    _qr, one_missing = populate_questionnaire(
        q, patient_with_city, [patient_with_city])

    assert len(issues) == 4, [i["linkId"] for i in issues]
    assert [i["linkId"] for i in one_missing] == [
        "demographics.address-postal-code"]


# ---------------------------------------------------------------------------
# 5. The two mechanisms the matrix above never reached (QA review of #576)
# ---------------------------------------------------------------------------

LOINC = "http://loinc.org"

UNSUPPORTED_LIST_DEF = ("http://hl7.org/fhir/StructureDefinition/Immunization"
                        "#Immunization.vaccineCode.text")
UNSUPPORTED_DATE_DEF = ("http://hl7.org/fhir/StructureDefinition/Immunization"
                        "#Immunization.occurrenceDateTime")


def _unsupported_list_questionnaire():
    return {
        "resourceType": "Questionnaire", "id": "unsupported-list",
        "status": "active",
        "item": [{
            "linkId": "immunizations", "type": "group", "repeats": True,
            "item": [
                {"linkId": "immunizations.vaccine", "type": "string",
                 "definition": UNSUPPORTED_LIST_DEF},
                {"linkId": "immunizations.date", "type": "date",
                 "definition": UNSUPPORTED_DATE_DEF},
            ],
        }],
    }


def test_a_repeating_group_of_an_unsupported_type_reports_every_leaf():
    """THE SHAPE THAT USED TO REPORT NOTHING AT ALL (QA review of #576).

    A `definition` leaf inside a `repeats: true` group is an attempted
    population whatever type it names. The engine used to read it narrower:
    `_list_group_resource_type` recognises only MedicationRequest /
    AllergyIntolerance / Condition, so a group naming anything else was not a
    list group, fell through to ordinary-group recursion, and its leaves
    reached `_resolve_answer` with no expression and no code — the "NOTHING
    WAS ATTEMPTED" branch. A Questionnaire asking for immunizations came back
    with two unanswered leaves, zero issues, and no `issues` parameter on the
    response at all. That is precisely the silence the ruling set out to
    remove: a caller reads it and concludes the patient has no immunizations.

    The predicate is now questionnaire structure alone, so this holds for
    ANY type the engine has no resolver for — which is why this file no
    longer keeps a copy of the engine's type table. What a fourth supported
    type would change is the answers on the next assertion, never these
    issues.

    Not a leak, before and after: the outcome is a function of the
    QUESTIONNAIRE's structure only — same shape, same result, on every
    patient — which is what the loop over PATIENT_MATRIX states.
    """
    q = _unsupported_list_questionnaire()

    for label, patient in PATIENT_MATRIX.items():
        qr, issues = populate_questionnaire(q, patient, [patient])

        leaf_ids = [leaf["linkId"] for leaf in _leaf_occurrences(qr["item"])]
        assert leaf_ids == ["immunizations.vaccine", "immunizations.date"], (
            f"[{label}] {leaf_ids}")
        assert not [leaf for leaf in _leaf_occurrences(qr["item"])
                    if "answer" in leaf], label
        # Two unanswered definition leaves, two issues, each naming itself.
        assert [i["linkId"] for i in issues] == [
            "immunizations.vaccine", "immunizations.date"], (
            f"[{label}] an unanswered leaf came back silent: {issues}")


def test_the_property_holds_on_the_unsupported_list_shape_too():
    """The biconditional, evaluated on the shape above.

    Before the fix, `_attempted_link_ids` and the engine disagreed on
    exactly this shape — the helper carried the ruling's wording, the engine
    carried a resource-type table — and no fixture here exercised it, so the
    docstring's "implementation-independent" claim went untested exactly
    where it was wrong. Both sides now read "attempted" the same way, and
    neither reads it from a list of types.
    """
    q = _unsupported_list_questionnaire()
    for label, patient in PATIENT_MATRIX.items():
        qr, issues = populate_questionnaire(q, patient, [patient])
        _assert_zero_bits(q, qr, issues, f"unsupported-list / {label}")


# --- a nested group inside a repeating list group ---------------------------

NESTED_ALLERGEN_DEF = ("http://hl7.org/fhir/StructureDefinition/"
                       "AllergyIntolerance#AllergyIntolerance.code.text")
NESTED_REACTION_DEF = (
    "http://hl7.org/fhir/StructureDefinition/AllergyIntolerance"
    "#AllergyIntolerance.reaction.manifestation.text")


def _nested_list_questionnaire():
    """A supported list group whose second leaf sits one group deeper."""
    return {
        "resourceType": "Questionnaire", "id": "nested-list",
        "status": "active",
        "item": [{
            "linkId": "allergies.item", "type": "group", "repeats": True,
            "item": [
                {"linkId": "a.allergen", "type": "string",
                 "definition": NESTED_ALLERGEN_DEF},
                {"linkId": "a.reactions", "type": "group", "item": [
                    {"linkId": "a.reactions.text", "type": "string",
                     "definition": NESTED_REACTION_DEF}]},
            ],
        }],
    }


def _allergy(resource_id, code=None, manifestation=None):
    resource = {"resourceType": "AllergyIntolerance", "id": resource_id,
                "patient": {"reference": "Patient/p1"}}
    if code:
        resource["code"] = {"text": code}
    if manifestation:
        resource["reaction"] = [{"manifestation": [{"text": manifestation}]}]
    return resource


def test_a_nested_group_inside_a_repeating_list_group_keeps_its_leaves():
    """THE SECOND SHAPE THAT REPORTED NOTHING (QA addendum to the review).

    `_populate_list_group` used to iterate a row's DIRECT children and test
    `child.get("definition")`. A child that is itself a group has none, so it
    was emitted as a bare item and its own leaves were never visited: a
    stored `reaction.manifestation.text` and the leaf asking for it were
    both absent from the response — no answer, no issue, no item.

    Worse than silence, it was invisible to the property: a leaf that is not
    in the response is neither answered nor unanswered, so the biconditional
    held over a hole. `_assert_zero_bits`'s structural mirror is what makes
    that case red now; this test states the same thing as the values a
    caller actually gets.
    """
    q = _nested_list_questionnaire()
    content = [_allergy("a1", code="Penicillin", manifestation="Hives")]

    qr, issues = populate_questionnaire(
        q, {"resourceType": "Patient", "id": "p1"}, content)

    row = qr["item"][0]
    assert row["linkId"] == "allergies.item"
    nested = next(c for c in row["item"] if c["linkId"] == "a.reactions")
    assert [leaf["linkId"] for leaf in _leaf_occurrences([row])] == [
        "a.allergen", "a.reactions.text"]
    assert nested["item"][0]["answer"] == [{"valueString": "Hives"}]
    assert issues == []


def test_a_nested_list_leaf_that_resolves_nothing_names_itself():
    """The other half: the nested leaf reports like any other attempted one.

    One labelled allergy with no reaction, one with neither. Row count still
    equals record count, and every unanswered definition leaf — at whatever
    depth — is in the issue list once per occurrence.
    """
    q = _nested_list_questionnaire()
    content = [_allergy("a1", code="Penicillin"), _allergy("a2")]

    qr, issues = populate_questionnaire(
        q, {"resourceType": "Patient", "id": "p1"}, content)

    assert len(qr["item"]) == 2, "row count must equal record count"
    assert Counter(i["linkId"] for i in issues) == Counter({
        "a.reactions.text": 2, "a.allergen": 1})


def test_the_property_holds_on_the_nested_list_shape_too():
    """The biconditional and the structural mirror, over record shapes.

    MUTATION: revert `_populate_list_children` to iterating direct children
    -> `a.reactions.text` disappears from every row and the mirror goes red
    on all four shapes, including the empty one.
    """
    q = _nested_list_questionnaire()
    shapes = {
        "nothing stored": [],
        "labelled, with a reaction": [
            _allergy("a1", code="Penicillin", manifestation="Hives")],
        "labelled, no reaction": [_allergy("a1", code="Penicillin")],
        "two unlabelled": [_allergy("a1"), _allergy("a2")],
    }
    patient = {"resourceType": "Patient", "id": "p1"}
    for label, content in shapes.items():
        qr, issues = populate_questionnaire(q, patient, content)
        _assert_zero_bits(q, qr, issues, f"nested-list / {label}")


# --- the explanation, said once ---------------------------------------------

def test_the_explanation_is_said_once_however_many_leaves_are_unanswered():
    """FINDING 2. The sentence is a constant; the linkId is what varies.

    Emitting the explanation on every leaf — 230 characters then, 285 now —
    made the response grow with the number of unanswered leaves TIMES a
    fixed paragraph, measured at 3519.6KB back from a 29.3KB request over
    HTTP. The ruling was to stop repeating it rather
    than to cap the list, so the shape is: one `informational` issue
    carrying the reason, then one `incomplete` issue per unanswered leaf
    carrying ONLY its linkId.

    MUTATION: put the sentence back into each per-leaf `diagnostics` (or
    prefix it, or append it) -> the count assertion goes red at 51 rather
    than 1, and the per-leaf equality goes red naming the leaf. Drop the
    summary issue -> the count goes red at 0 and the caller loses the only
    statement of why.
    """
    issues = [{"linkId": f"section.item.leaf-{n}"} for n in range(50)]

    outcome = _issues_outcome(issues)
    rendered = json.dumps(outcome)

    summary = [i for i in outcome["issue"] if i["code"] == "informational"]
    per_leaf = [i for i in outcome["issue"] if i["code"] == "incomplete"]

    assert len(summary) == 1
    assert summary[0]["diagnostics"] == NOT_POPULATED
    assert rendered.count("%patient projection") == 1, (
        "the explanation is repeated: it is the same constant for every "
        "leaf, and repeating it is what amplified the response")

    # Each per-leaf issue carries the linkId and nothing else, so it is
    # greppable on its own — which is what a caller branches on.
    assert len(per_leaf) == 50
    assert [i["diagnostics"] for i in per_leaf] == [
        i["linkId"] for i in issues]
    assert {i["severity"] for i in outcome["issue"]} == {"information"}


def test_the_response_grows_with_the_leaf_count_not_with_a_paragraph():
    """The amplification, stated as the shape of the growth rather than a cap.

    Ten times the leaves must cost about ten times the bytes plus a
    constant. With the sentence on every issue the per-leaf cost carried a
    fixed paragraph, so this ratio was pinned to the paragraph instead of
    to the data.

    MUTATION: restore the per-leaf sentence -> the marginal cost per leaf
    rises past the bound and this goes red.
    """
    def rendered_bytes(n):
        return len(json.dumps(_issues_outcome(
            [{"linkId": f"section.item.leaf-{i}"} for i in range(n)])))

    small, large = rendered_bytes(10), rendered_bytes(1000)
    per_leaf = (large - small) / 990

    # The bound is the explanation itself, which is the honest way to state
    # it: one more unanswered leaf must cost less than one more copy of the
    # reason. Today that is ~91 bytes (a 21-character linkId plus the issue's
    # JSON envelope) against a ~253-character constant. An absolute number
    # here would be a second thing to keep true.
    assert per_leaf < len(NOT_POPULATED), (
        f"{per_leaf:.1f} bytes per unanswered leaf, against a "
        f"{len(NOT_POPULATED)}-character explanation: the reason is being "
        f"repeated per leaf again")


# --- the item.code Observation path -----------------------------------------

def _observation(code, **value):
    resource = {
        "resourceType": "Observation", "id": f"o-{code}", "status": "final",
        "subject": {"reference": "Patient/p1"},
        "effectiveDateTime": "2026-01-01",
        "code": {"coding": [{"system": LOINC, "code": code}]},
    }
    resource.update(value)
    return resource


#: Every shape `_observation_answer` can be handed, including the two that
#: matter after `apply_redaction` has run: a valueCodeableConcept whose `text`
#: was stripped and whose code r6/terminology.py has no label for, and a
#: value[x] type the resolver does not read. Both resolve to nothing on a
#: record that EXISTS, which is the case a record-driven classifier would
#: treat differently from "no Observation at all".
_OBSERVATION_CASES = {
    "matched, valueQuantity": (True, [
        _observation("29463-7", valueQuantity={"value": 70, "unit": "kg"})]),
    "matched, valueString": (True, [
        _observation("29463-7", valueString="70 kg")]),
    "matched, valueCodeableConcept with text": (True, [
        _observation("29463-7", valueCodeableConcept={"text": "Normal"})]),
    "matched, valueCodeableConcept stripped of text": (False, [
        _observation("29463-7", valueCodeableConcept={
            "coding": [{"system": SNOMED, "code": "17621005"}]})]),
    "matched, no value[x] at all": (False, [_observation("29463-7")]),
    "matched, unsupported value[x] type": (False, [
        _observation("29463-7", valueBoolean=True)]),
    "a different code": (False, [
        _observation("8302-2", valueString="170 cm")]),
    "no observations at all": (False, []),
}


def test_the_item_code_path_holds_the_property():
    """The third population mechanism, which nothing else in this file reaches.

    The intake Questionnaire has no `item.code` items, so `_report_unpopulated`
    on the Observation branch is invisible to every other fixture here — the
    engine's own comment says so. A classifier re-introduced on that branch
    alone would pass this whole file without these two tests.
    """
    q = {"resourceType": "Questionnaire", "id": "obs-q", "status": "active",
         "item": [{"linkId": "weight", "type": "string",
                   "code": [{"system": LOINC, "code": "29463-7"}]}]}

    for label, (should_answer, content) in _OBSERVATION_CASES.items():
        qr, issues = populate_questionnaire(q, FULL_PATIENT, content)
        _assert_zero_bits(q, qr, issues, label)

        leaf = qr["item"][0]
        assert ("answer" in leaf) is should_answer, f"[{label}] {leaf}"
        assert [i["linkId"] for i in issues] == ([] if should_answer
                                                 else ["weight"]), label


def test_the_item_code_issue_says_nothing_about_the_observation():
    """Five different stored records, all unanswerable, one identical issue.

    An Observation that exists but carries an unlabelled coded value has to be
    indistinguishable from no Observation at all. Anything else is a read of
    the record through the issue channel — the shape of the leak this file
    exists to prevent, one mechanism over from where it was found.
    """
    q = {"resourceType": "Questionnaire", "id": "obs-q", "status": "active",
         "item": [{"linkId": "weight", "type": "string",
                   "code": [{"system": LOINC, "code": "29463-7"}]}]}

    payloads = set()
    for _label, (should_answer, content) in _OBSERVATION_CASES.items():
        if should_answer:
            continue
        _qr, issues = populate_questionnaire(q, FULL_PATIENT, content)
        payloads.add(repr(issues))

    assert len(payloads) == 1, (
        f"the issue payload varies with the stored Observation: {payloads}")
