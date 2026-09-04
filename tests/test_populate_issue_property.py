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

from collections import Counter

from r6.sdc.intake import intake_questionnaire
from r6.sdc.populate import NOT_POPULATED, populate_questionnaire

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

#: The resource types list-group population actually implements. This is a
#: LITERAL, deliberately, not an import of r6/sdc/populate.py's
#: `_LIST_RESOURCE_CONFIG` — the repo's two-file convention (cf.
#: `_UNREDACTED_EXITS` and tests/test_unredacted_exits.py). Adding a fourth
#: type to the engine goes red here until someone updates this set, which is
#: the point of writing it twice.
#:
#: It is here because "a list-group leaf with a `definition`" turned out to
#: mean two different things. The engine reads it as "a leaf of a group whose
#: leaves name a type in that table" — `_list_group_resource_type` returns
#: None for anything else, the group falls through to ordinary-group
#: recursion, and its leaves are then not attempted at all. The first version
#: of this helper read it as "any `definition` leaf under any repeating
#: group", which is the CTO addendum's literal wording. The two disagree on
#: exactly one shape, pinned below by
#: test_a_repeating_group_of_an_unsupported_type_is_silent, and no fixture
#: here exercised it, so the disagreement was invisible. QA review of #576.
LIST_POPULATED_TYPES = {"MedicationRequest", "AllergyIntolerance", "Condition"}


def _list_group_type(item):
    """The resource type this repeating group's leaves populate from, or None.

    Mirrors r6/sdc/populate.py:_list_group_resource_type — the FIRST child
    definition naming a supported type wins, and a group naming none is not a
    list group at all.
    """
    if not item.get("repeats"):
        return None
    for child in item.get("item", []):
        definition = child.get("definition") or ""
        if "#" not in definition:
            continue
        rtype = definition.split("#", 1)[1].split(".", 1)[0]
        if rtype in LIST_POPULATED_TYPES:
            return rtype
    return None


def _attempted_link_ids(questionnaire):
    """linkIds where the questionnaire asks populate to resolve something.

    Read from the Questionnaire, not from populate.py, so this test still
    means something if the engine's own predicate changes. Three mechanisms,
    matching the module docstring of r6/sdc/populate.py:

      - an initialExpression (FHIRPath against the %patient projection),
      - an `item.code` (Observation matching),
      - a `definition` on a leaf of a repeating group that names one of
        LIST_POPULATED_TYPES (list-resource population).

    `definition` alone is deliberately not enough: every demographics leaf on
    the intake form carries one for `$extract`'s benefit and populates by
    expression. A definition-bearing leaf outside a repeating group is not an
    attempted population — and neither, today, is one inside a repeating
    group naming a type the engine does not implement. See
    LIST_POPULATED_TYPES for why that second clause is stated out loud.
    """
    found = set()
    for item in questionnaire.get("item", []):
        if item.get("type") == "group":
            if _list_group_type(item):
                for child in item.get("item", []):
                    if child.get("definition"):
                        found.add(child["linkId"])
            else:
                found |= _attempted_link_ids(item)
            continue
        if _has_initial_expression(item) or item.get("code"):
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


def _assert_zero_bits(questionnaire, qr, issues, label):
    """THE PROPERTY. Per linkId: unanswered attempted occurrences == issues.

    MUTATION: silence any attempted leaf (re-add a classifier, of any shape)
    -> the expected count exceeds the actual and this goes red naming the
    linkId. Emit for an unattempted leaf -> the second assertion goes red.
    """
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

    # Every issue says the same thing, and it is the neutral sentence.
    for issue in issues:
        assert issue["detail"] == NOT_POPULATED, issue


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
    """
    withheld_q = _expression_questionnaire({"mrn": WITHHELD_EXPRESSIONS["mrn"]})
    absent_q = _expression_questionnaire(ABSENT_BUT_ALLOWLISTED)

    _qr, withheld_issues = populate_questionnaire(
        withheld_q, SPARSE_PATIENT, [SPARSE_PATIENT])
    _qr, absent_issues = populate_questionnaire(
        absent_q, SPARSE_PATIENT, [SPARSE_PATIENT])

    assert len(withheld_issues) == len(absent_issues) == 1
    assert withheld_issues[0]["detail"] == absent_issues[0]["detail"]

    detail = withheld_issues[0]["detail"]
    # The allowlist is public and is the actionable half.
    for element in ("name", "birthDate", "gender", "telecom", "address"):
        assert element in detail, element
    # Nothing that reads as a claim about this patient or this request.
    for forbidden in ("withheld", "refused", "denied", "not permitted",
                      "does not have", "no such"):
        assert forbidden not in detail.lower(), forbidden


def test_the_expression_text_is_never_echoed_back():
    """A `where()` clause is a place a questionnaire author parks a literal;
    the OperationOutcome leaves the boundary. Pre-existing property of the
    fixed sentence, pinned here because the retext could have lost it."""
    expression = "%patient.identifier.value.where($this='a-planted-literal')"
    q = _expression_questionnaire({"planted": expression})

    _qr, issues = populate_questionnaire(q, FULL_PATIENT, [FULL_PATIENT])

    assert len(issues) == 1
    assert "planted" not in issues[0]["detail"]
    assert expression not in issues[0]["detail"]


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


def test_a_repeating_group_of_an_unsupported_type_is_silent():
    """THE ONE PLACE THE RULING AND THE ENGINE READ THE SAME WORDS DIFFERENTLY.

    The CTO addendum states the exclusion predicate as: the issue attaches
    where population was attempted — "an `initialExpression`, an `item.code`
    Observation match, or a list-group leaf with a `definition`". A
    `definition` leaf inside a `repeats: true` group satisfies that wording
    whatever type it names.

    The engine reads it narrower. `_list_group_resource_type` only recognises
    a group whose leaves name MedicationRequest / AllergyIntolerance /
    Condition; anything else is not a list group, falls through to
    ordinary-group recursion, and its leaves reach `_resolve_answer` with no
    expression and no code — the "NOTHING WAS ATTEMPTED" branch. So a
    Questionnaire asking for immunizations gets unanswered leaves and NO
    issue naming them: silence, which is the state the ruling set out to
    remove ("a caller reads silence and concludes the patient has no such
    value").

    This test pins WHAT THE ENGINE DOES TODAY, not what it should do. It is
    deliberately a plain assertion rather than a strict xfail — CLAUDE.md
    records a strict-xfail row going red on the day someone fixed what it
    pinned. If the reading is widened so these leaves report, this test is
    one of the two places to update; LIST_POPULATED_TYPES is the other.

    Not a leak: the silence is a function of the QUESTIONNAIRE's structure
    only — same shape, same result, on every patient — which is what the loop
    over PATIENT_MATRIX states.
    """
    q = _unsupported_list_questionnaire()

    for label, patient in PATIENT_MATRIX.items():
        qr, issues = populate_questionnaire(q, patient, [patient])

        leaf_ids = [leaf["linkId"] for leaf in _leaf_occurrences(qr["item"])]
        assert leaf_ids == ["immunizations.vaccine", "immunizations.date"], (
            f"[{label}] {leaf_ids}")
        assert not [leaf for leaf in _leaf_occurrences(qr["item"])
                    if "answer" in leaf], label
        # The divergence, stated as the number it is: two unanswered
        # definition leaves, zero issues.
        assert issues == [], (
            f"[{label}] the engine now reports unsupported-type list leaves — "
            f"good, but LIST_POPULATED_TYPES and this test have to be updated "
            f"together: {issues}")


def test_the_property_holds_on_the_unsupported_list_shape_too():
    """The biconditional, evaluated on the shape above.

    It passes because `_attempted_link_ids` now reads "attempted" the way the
    engine does. Before the QA review of #576 the helper called those two
    leaves attempted while the engine did not, so this assertion would have
    failed — and no fixture in this file exercised the shape, so the helper's
    "implementation-independent" claim went untested exactly where it was
    wrong.
    """
    q = _unsupported_list_questionnaire()
    for label, patient in PATIENT_MATRIX.items():
        qr, issues = populate_questionnaire(q, patient, [patient])
        _assert_zero_bits(q, qr, issues, f"unsupported-list / {label}")


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
