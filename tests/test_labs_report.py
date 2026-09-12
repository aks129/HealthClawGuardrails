# tests/test_labs_report.py
from r6.labs.interpret import interpret_observation
from r6.labs.report import (
    annotate_observation, build_interpretation_summary, build_consumer_summary,
)

V3 = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"


def _obs(loinc, value, unit):
    return {"resourceType": "Observation", "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": loinc}]},
            "valueQuantity": {"value": value, "unit": unit}}


def test_annotate_adds_interpretation_codeableconcept():
    obs = _obs("2823-3", 7.0, "mmol/L")
    out = annotate_observation(obs, interpret_observation(obs))
    coding = out["interpretation"][0]["coding"][0]
    assert coding["system"] == V3 and coding["code"] == "HH"
    assert obs.get("interpretation") is None  # original untouched (copy)


def test_annotate_stamps_table_range_but_not_resource_range():
    obs = _obs("2823-3", 4.2, "mmol/L")
    out = annotate_observation(obs, interpret_observation(obs))
    assert out["referenceRange"][0]["low"]["value"] == 3.5
    assert "HealthClaw" in out["referenceRange"][0]["text"]


def test_annotate_omits_interpretation_when_indeterminate():
    obs = _obs("9999-9", 1, "mmol/L")
    out = annotate_observation(obs, interpret_observation(obs))
    assert "interpretation" not in out


def test_interpretation_summary_counts():
    results = [interpret_observation(_obs("2823-3", 7.0, "mmol/L")),   # HH critical
               interpret_observation(_obs("2823-3", 4.2, "mmol/L")),   # N
               interpret_observation(_obs("9999-9", 1, "mmol/L"))]     # indeterminate
    s = build_interpretation_summary(results)
    assert s["critical"] == 1 and s["normal"] == 1 and s["indeterminate"] == 1
    assert any(f["flag"] == "HH" for f in s["flagged"])


def test_consumer_summary_is_plain_and_has_next_step():
    results = [interpret_observation(_obs("2823-3", 7.0, "mmol/L"))]
    c = build_consumer_summary(results)
    text = " ".join(line["message"] for line in c["lines"]).lower()
    assert "potassium" in text and "clinician" in text
    for banned in ("diagnos", "prescrib", "treatment"):
        assert banned not in text


# ---------------------------------------------------------------------------
# An unscored analyte is not a clean result (#689). Same posture as
# r6/caregaps/report.py, whose `_unevaluated_marker` docstring already names
# this module's `_indeterminate` as the pattern it followed.
# ---------------------------------------------------------------------------

def test_a_panel_of_only_unscored_analytes_does_not_come_back_empty():
    """The measured case: a patient whose only readings were blood pressures
    produced a summary a clinician read as "nothing flagged".

    Blood pressure is multi-component, so it is never in LOINC_RANGES and the
    interpreter is right to decline it. What is wrong is the silence that
    follows: "we did not evaluate this" and "we evaluated this and it is
    fine" rendered identically.

    MUTATION: drop the marker from build_consumer_summary -> red.
    """
    results = [interpret_observation(_obs("8480-6", 164, "mm[Hg]")),
               interpret_observation(_obs("8462-4", 98, "mm[Hg]"))]
    assert all(r["flag"] is None for r in results), "premise: neither scores"

    consumer = build_consumer_summary(results)

    assert consumer["unevaluated_count"] == 2
    assert consumer["unevaluated"]
    note = consumer["unevaluated_note"].lower()
    assert "not" in note and ("evaluat" in note or "check" in note)
    # A limit on the check, never a finding about the person.
    for forbidden in ("normal", "within the typical range", "no concerns",
                      "nothing"):
        assert forbidden not in note


def test_the_unevaluated_note_names_what_was_not_evaluated():
    results = [interpret_observation(_obs("8480-6", 164, "mm[Hg]"))]

    consumer = build_consumer_summary(results)

    assert "Systolic blood pressure" in consumer["unevaluated_analytes"]
    assert "systolic blood pressure" in consumer["unevaluated_note"].lower()


def test_a_partly_scored_panel_still_says_what_was_left_undecided():
    """All-or-nothing is the wrong granularity: care gaps paid for that once
    (#417), where sex-gated screenings were dropped in silence beside
    screenings that reported.

    MUTATION: attach the marker only when `lines` is empty -> red.
    """
    results = [interpret_observation(_obs("2823-3", 7.0, "mmol/L")),  # scores HH
               interpret_observation(_obs("8480-6", 164, "mm[Hg]"))]  # does not

    consumer = build_consumer_summary(results)

    assert consumer["lines"], "the scored analyte still reports"
    assert consumer["unevaluated_count"] == 1
    assert "Systolic blood pressure" in consumer["unevaluated_analytes"]


def test_a_fully_scored_panel_carries_no_marker():
    """The marker is a statement about a limit. Attaching one to a whole
    answer would make it noise, and then it would be ignored when it matters.
    """
    results = [interpret_observation(_obs("2823-3", 4.2, "mmol/L"))]

    consumer = build_consumer_summary(results)

    assert "unevaluated" not in consumer
    assert "unevaluated_count" not in consumer


def test_the_clinician_summary_names_the_undecided_analytes():
    """A count alone makes the reader go and find out which. The flagged list
    is what a caller reads; it must not be readable as completeness while
    something went unscored."""
    results = [interpret_observation(_obs("2823-3", 4.2, "mmol/L")),
               interpret_observation(_obs("8480-6", 164, "mm[Hg]"))]

    summary = build_interpretation_summary(results)

    assert summary["indeterminate"] == 1
    assert summary["indeterminate_analytes"] == ["Systolic blood pressure"]
    assert summary["flagged"] == [], "premise: nothing scored abnormal"


def test_an_unscored_analyte_never_enters_the_flagged_list():
    """It is not a finding. It is the absence of one, and the two must stay
    apart — a caller that treats the flagged list as findings would otherwise
    report a blood pressure as abnormal on no evidence."""
    results = [interpret_observation(_obs("8480-6", 164, "mm[Hg]"))]

    summary = build_interpretation_summary(results)

    assert summary["flagged"] == []
    assert summary["high"] == 0 and summary["critical"] == 0
    assert summary["indeterminate"] == 1
