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

def _attempted_link_ids(questionnaire, parent_repeats=False):
    """linkIds where the questionnaire asks populate to resolve something.

    Read from the Questionnaire, not from populate.py, so this test still
    means something if the engine's own predicate changes. Three mechanisms,
    matching the module docstring of r6/sdc/populate.py:

      - an initialExpression (FHIRPath against the %patient projection),
      - an `item.code` (Observation matching),
      - a `definition` on a leaf of a REPEATING group (list-resource
        population).

    `definition` alone is deliberately not enough: every demographics leaf on
    the intake form carries one for `$extract`'s benefit and populates by
    expression. A definition-bearing leaf outside a repeating group is not an
    attempted population.
    """
    found = set()
    for item in questionnaire.get("item", []):
        if item.get("type") == "group":
            found |= _attempted_link_ids(item, bool(item.get("repeats")))
            continue
        if _has_initial_expression(item) or item.get("code"):
            found.add(item["linkId"])
        elif parent_repeats and item.get("definition"):
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
