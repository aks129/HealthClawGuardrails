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
